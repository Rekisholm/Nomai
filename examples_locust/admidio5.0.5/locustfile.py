import os
import random
import re
import sys
import time
from pathlib import Path

from locust import HttpUser, between, events, task
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)


# ========== Configuration ==========
HOST = os.getenv("ADMIDIO_HOST", "http://localhost")
PORT = int(os.getenv("ADMIDIO_PORT", "3100"))
BASE_URL = os.getenv("ADMIDIO_BASE", f"{HOST}:{PORT}").rstrip("/")
USERNAME = os.getenv("ADMIDIO_USERNAME", "admin")
PASSWORD = os.getenv("ADMIDIO_PASSWORD", "Aa!123456")
TIMEOUT = int(os.getenv("ADMIDIO_TIMEOUT", "10"))


UUID_RE = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"


def extract_csrf_token(html):
    match = re.search(r'name="adm_csrf_token"[^>]*value="([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'value="([^"]+)"[^>]*name="adm_csrf_token"', html)
    if match:
        return match.group(1)
    return None


def normalize_url_path(path):
    path = re.sub(r"([?&](?:user_uuid|msg_uuid|list_uuid|role_list)=)" + UUID_RE, r"\1:uuid", path)
    path = re.sub(r"/" + UUID_RE, "/:uuid", path)
    path = re.sub(r"timestamp=[^&]+", "timestamp=:ts", path)
    path = re.sub(r"_=\d+", "_=:ts", path)
    return path


class AdmidioUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"
                ),
            }
        )
        self._login()

    def _send_request(self, method, url, name=None, expected_statuses=None, **kwargs):
        full_url = f"{BASE_URL}{url}" if url.startswith("/") else url
        stat_name = name if name else normalize_url_path(url)
        expected_statuses = expected_statuses or {200, 302}
        kwargs.setdefault("timeout", TIMEOUT)
        start_time = time.time()

        try:
            resp = self.session.request(method, full_url, **kwargs)
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

    def _login(self):
        resp = self._send_request("GET", "/modules/overview.php")
        token = extract_csrf_token(resp.text)
        if not token:
            raise Exception("Unable to extract adm_csrf_token from the overview page")

        resp = self._send_request(
            "POST",
            "/system/login.php?mode=check",
            data={
                "adm_csrf_token": token,
                "plg_usr_login_name": USERNAME,
                "plg_usr_password": PASSWORD,
            },
            headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/modules/overview.php"},
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise Exception(f"Login response is not JSON: HTTP {resp.status_code}") from exc
        if payload.get("status") != "success":
            raise Exception(f"Admidio Login failed: {resp.text[:200]}")

    def _message_datatable_params(self, length="10"):
        params = {
            "draw": "1",
            "order[0][column]": "4",
            "order[0][dir]": "desc",
            "start": "0",
            "length": length,
            "search[value]": "",
            "search[regex]": "false",
            "_": str(int(time.time() * 1000)),
        }
        for i in range(6):
            params[f"columns[{i}][data]"] = str(i)
            params[f"columns[{i}][name]"] = ""
            params[f"columns[{i}][searchable]"] = "true"
            params[f"columns[{i}][orderable]"] = "false" if i in (2, 5) else "true"
            params[f"columns[{i}][search][value]"] = ""
            params[f"columns[{i}][search][regex]"] = "false"
        return params

    def _get_messages(self):
        resp = self._send_request(
            "GET",
            "/modules/messages/messages_data.php",
            params=self._message_datatable_params(),
            name="/modules/messages/messages_data.php",
        )
        try:
            payload = resp.json()
        except ValueError:
            return []
        if "error" in payload:
            raise Exception(f"Message list API returned an error: {payload['error'][:200]}")
        return payload.get("data") or []

    @task(3)
    def browse_modules(self):
        for path in (
            "/modules/overview.php",
            "/modules/messages/messages.php",
            "/modules/announcements.php",
            "/modules/events/events.php",
            "/modules/contacts/contacts.php",
            "/modules/documents-files.php",
        ):
            self._send_request("GET", path)

    @task(2)
    def messages_flow(self):
        self._send_request("GET", "/modules/messages/messages.php")
        messages = self._get_messages()
        if not messages:
            return

        item = random.choice(messages)
        link_html = item.get("1", "")
        match = re.search(r'href="([^"]+messages_write\.php\?msg_uuid=[^"]+)"', link_html)
        if match:
            self._send_request("GET", match.group(1), name="/modules/messages/messages_write.php?msg_uuid=:uuid")

    @task(1)
    def delete_one_message_if_available(self):
        messages = self._get_messages()
        if not messages:
            return

        item = random.choice(messages)
        action_html = item.get("5", "")
        match = re.search(
            r"callUrlHideElement\('([^']+)',\s*'([^']+)',\s*'([^']+)'",
            action_html,
        )
        if not match:
            return

        row_id, delete_url, csrf_token = match.groups()
        msg_match = re.search(r"msg_uuid=(" + UUID_RE + ")", delete_url)
        if not msg_match:
            return

        self._send_request(
            "POST",
            f"/modules/messages/messages.php?msg_uuid={msg_match.group(1)}",
            data={"adm_csrf_token": csrf_token, "uuid": row_id, "mode": ""},
            headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/modules/messages/messages.php"},
            name="/modules/messages/messages.php?msg_uuid=:uuid",
            expected_statuses={200, 302, 404},
        )

    @task(2)
    def groups_roles_list_flow(self):
        roles_page = self._send_request("GET", "/modules/groups-roles/groups_roles.php")
        roles = re.findall(r"role_list=(" + UUID_RE + ")", roles_page.text)
        if not roles:
            return

        mylist_page = self._send_request("GET", "/modules/groups-roles/mylist.php")
        token = extract_csrf_token(mylist_page.text)
        if not token:
            raise Exception("Unable to extract adm_csrf_token from the mylist page")

        role_uuid = random.choice(roles)
        resp = self._send_request(
            "POST",
            "/modules/groups-roles/mylist_function.php?mode=save_temporary",
            data={
                "adm_csrf_token": token,
                "column[]": ["usr_uuid", "usr_login_name"],
                "sort[]": ["", ""],
                "condition[]": ["", ""],
                "sel_roles[]": role_uuid,
            },
            headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}/modules/groups-roles/mylist.php"},
        )
        try:
            payload = resp.json()
        except ValueError:
            return
        final_url = payload.get("url")
        if payload.get("status") == "success" and final_url:
            self._send_request("GET", final_url, name="/modules/groups-roles/lists_show.php?list_uuid=:uuid")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== Admidio 5.0.5 Locust test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Login user: {USERNAME}")
