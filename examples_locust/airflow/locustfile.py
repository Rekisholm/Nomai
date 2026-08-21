import random
import re
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from locust import HttpUser, between, events, task

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger
from util import html_2_csrf


REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)

PORT = 8080
HOST = f"http://localhost:{PORT}"

ATTACK_DAG_ID = "example_trigger_target_dag"

DAG_IDS = [
    ATTACK_DAG_ID, "example_bash_operator", "example_branch_dop_operator_v3",
    "example_branch_operator", "example_complex", "example_external_task_marker_child",
    "example_external_task_marker_parent", "example_http_operator", "example_nested_branch_dag",
    "example_passing_params_via_test_command", "example_pig_operator", "example_python_operator",
    "example_short_circuit_operator", "example_skip_dag", "example_subdag_operator",
    "example_trigger_controller_dag", "example_xcom", "latest_only",
    "latest_only_with_trigger", "test_utils", "tutorial"
]
NORMAL_MUTATION_DAG_IDS = [dag_id for dag_id in DAG_IDS if dag_id != ATTACK_DAG_ID]


def random_suffix(length=6):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def parse_record_id(html, value, field_name):
    soup = BeautifulSoup(html, "html.parser")
    row = soup.find("tr", string=re.compile(re.escape(value)))
    if row is None:
        text_node = soup.find(string=re.compile(re.escape(value)))
        row = text_node.find_parent("tr") if text_node else None
    if row:
        checkbox = row.find("input", {"name": field_name})
        if checkbox and checkbox.get("value"):
            return checkbox["value"]
        link = row.find("a", href=re.compile(r"/delete/|\?id="))
        if link and link.get("href"):
            match = re.search(r"[?&]id=(\d+)", link["href"])
            if match:
                return match.group(1)
    return None


class AirflowAdminUser(HttpUser):
    wait_time = between(1, 3)
    host = HOST

    def on_start(self):
        self.session = requests.Session()
        self.csrf_token = ""
        self.created_user_ids = []
        self.created_conn_ids = []
        self.refresh_csrf()

    def _request(self, method, path, *, name=None, expected=(200,), **kwargs):
        url = path if path.startswith("http") else urljoin(HOST, path)
        stat_name = name or path
        start_time = time.time()
        error = None
        resp = None
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, path)
            if resp.status_code not in expected:
                error = Exception(f"HTTP {resp.status_code}: {resp.text[:120]}")
            return resp
        except Exception as exc:
            resp = None
            error = exc
            raise
        finally:
            elapsed = (time.time() - start_time) * 1000
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=elapsed,
                response_length=len(resp.content) if resp is not None else 0,
                context={},
                exception=error,
            )

    def refresh_csrf(self):
        resp = self._request("GET", "/admin/", name="/admin/")
        if resp.status_code != 200:
            raise Exception(f"Home page access failed: {resp.status_code}")
        self.csrf_token = html_2_csrf(resp.text)
        if not self.csrf_token:
            raise Exception("Unable to extract CSRF token from the Airflow home page")

    def form_headers(self):
        return {"X-CSRFToken": self.csrf_token}

    @task(4)
    def browse_admin_pages(self):
        for path in random.sample([
            "/admin/",
            "/admin/dagrun/",
            "/admin/user/",
            "/admin/connection/",
            "/admin/configurationview/",
        ], k=3):
            self._request("GET", path, name=path)
            time.sleep(0.1)

    @task(4)
    def switch_dag_state(self):
        dag_id = random.choice(NORMAL_MUTATION_DAG_IDS)
        paused = random.choice(("true", "false"))
        self._request(
            "POST",
            "/admin/airflow/paused",
            name="/admin/airflow/paused",
            expected=(200, 302),
            params={"is_paused": paused, "dag_id": dag_id},
            headers=self.form_headers(),
            allow_redirects=False,
        )

    @task(2)
    def create_dagrun_form_flow(self):
        form_path = "/admin/dagrun/new/?url=%2Fadmin%2Fdagrun%2F"
        page = self._request("GET", form_path, name="/admin/dagrun/new/").text
        csrf = html_2_csrf(page) or self.csrf_token
        dag_id = random.choice(NORMAL_MUTATION_DAG_IDS)
        execution_date = (datetime.now(timezone.utc) + timedelta(seconds=random.randint(1, 600))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._request(
            "POST",
            form_path,
            name="/admin/dagrun/new/",
            expected=(200, 302),
            data={
                "csrf_token": csrf,
                "state": "running",
                "dag_id": dag_id,
                "execution_date": execution_date,
                "run_id": f"locust__{random_suffix(8)}",
                "external_trigger": "y",
            },
            allow_redirects=False,
        )

    @task(1)
    def create_and_delete_user(self):
        form_path = "/admin/user/new/?url=%2Fadmin%2Fuser%2F"
        page = self._request("GET", form_path, name="/admin/user/new/").text
        csrf = html_2_csrf(page) or self.csrf_token
        username = f"locust_user_{random_suffix()}"
        self._request(
            "POST",
            form_path,
            name="/admin/user/new/",
            expected=(200, 302),
            data={"csrf_token": csrf, "username": username, "email": f"{username}@example.test"},
            allow_redirects=False,
        )
        listing = self._request("GET", "/admin/user/", name="/admin/user/").text
        user_id = parse_record_id(listing, username, "id")
        if user_id:
            self.created_user_ids.append(user_id)
            self._delete_user(user_id)

    def _delete_user(self, user_id):
        if str(user_id) == "1":
            return
        self._request(
            "POST",
            "/admin/user/delete/",
            name="/admin/user/delete/",
            expected=(200, 302),
            data={"id": user_id, "url": "/admin/user/", "csrf_token": self.csrf_token},
            allow_redirects=False,
        )

    @task(1)
    def create_and_delete_connection(self):
        form_path = "/admin/connection/new/?url=%2Fadmin%2Fconnection%2F"
        page = self._request("GET", form_path, name="/admin/connection/new/").text
        csrf = html_2_csrf(page) or self.csrf_token
        conn_id = f"locust_conn_{random_suffix()}"
        self._request(
            "POST",
            form_path,
            name="/admin/connection/new/",
            expected=(200, 302),
            data={
                "csrf_token": csrf,
                "conn_id": conn_id,
                "conn_type": "__None",
                "host": "",
                "schema": "",
                "login": "",
                "password": "",
                "port": "",
                "extra": "",
                "extra__jdbc__drv_path": "",
                "extra__jdbc__drv_clsname": "",
                "extra__google_cloud_platform__project": "",
                "extra__google_cloud_platform__key_path": "",
                "extra__google_cloud_platform__keyfile_dict": "",
                "extra__google_cloud_platform__scope": "",
                "extra__google_cloud_platform__num_retries": "",
                "extra__grpc__auth_type": "",
                "extra__grpc__credentials_pem_file": "",
                "extra__grpc__scopes": "",
            },
            allow_redirects=False,
        )
        listing = self._request("GET", "/admin/connection/", name="/admin/connection/").text
        record_id = parse_record_id(listing, conn_id, "id")
        if record_id:
            self.created_conn_ids.append(record_id)
            self._delete_connection(record_id)

    def _delete_connection(self, record_id):
        self._request(
            "POST",
            "/admin/connection/delete/",
            name="/admin/connection/delete/",
            expected=(200, 302),
            data={"id": record_id, "url": "/admin/connection/", "csrf_token": self.csrf_token},
            allow_redirects=False,
        )
