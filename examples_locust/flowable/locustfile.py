import os
import re
import time
import uuid

from locust import HttpUser, between, events, task

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)
import requests


# ========== Configuration ==========
HOST = os.getenv("FLOWABLE_HOST", "http://localhost")
PORT = int(os.getenv("FLOWABLE_PORT", "8080"))
BASE_URL = os.getenv("FLOWABLE_BASE", f"{HOST}:{PORT}").rstrip("/")
USERNAME = os.getenv("FLOWABLE_USER", "admin")
PASSWORD = os.getenv("FLOWABLE_PASS", "test")
TIMEOUT = int(os.getenv("FLOWABLE_TIMEOUT", "10"))


def normalize_url_path(path):
    path = re.sub(r"version=\d+", "version=:ts", path)
    path = re.sub(
        r"/flowable-ui/app/rest/tasks/[^/?]+/action/complete",
        "/flowable-ui/app/rest/tasks/:id/action/complete",
        path,
    )
    path = re.sub(
        r"/flowable-ui/idm-app/rest/admin/users/[^/?]+",
        "/flowable-ui/idm-app/rest/admin/users/:id",
        path,
    )
    path = re.sub(
        r"/flowable-ui/modeler-app/rest/models/[^/?]+/parent-relations",
        "/flowable-ui/modeler-app/rest/models/:id/parent-relations",
        path,
    )
    path = re.sub(
        r"/flowable-ui/modeler-app/rest/models/[^/?]+",
        "/flowable-ui/modeler-app/rest/models/:id",
        path,
    )
    return path


class FlowableUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
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
        expected_statuses = expected_statuses or {200, 201, 204}
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
        self._send_request("GET", "/flowable-ui/idm/views/login.html")
        resp = self._send_request(
            "POST",
            "/flowable-ui/app/authentication",
            data={
                "j_username": USERNAME,
                "j_password": PASSWORD,
                "_spring_security_remember_me": "true",
                "submit": "Login",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/flowable-ui/idm/",
            },
        )
        if "FLOWABLE_REMEMBER_ME" not in self.session.cookies.get_dict():
            raise Exception(f"Flowable login did not succeed: HTTP {resp.status_code}")

    def _json_headers(self):
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE_URL,
        }

    @task(3)
    def task_flow(self):
        self._send_request("GET", "/flowable-ui/workflow/views/tasks.html")
        suffix = uuid.uuid4().hex[:8]
        resp = self._send_request(
            "POST",
            "/flowable-ui/app/rest/tasks",
            json={
                "name": f"New task {suffix}",
                "description": "normal traffic task",
            },
            headers=self._json_headers(),
        )
        task_id = resp.json()["id"]
        self._send_request(
            "PUT",
            f"/flowable-ui/app/rest/tasks/{task_id}/action/complete",
            headers={"Origin": BASE_URL},
        )

    @task(2)
    def user_flow(self):
        user_id = "u" + uuid.uuid4().hex[:10]
        created = False

        self._send_request("GET", "/flowable-ui/idm-app/rest/account")
        try:
            self._send_request(
                "POST",
                "/flowable-ui/idm-app/rest/admin/users",
                json={
                    "id": user_id,
                    "email": f"{user_id}@example.com",
                    "firstName": "Normal",
                    "lastName": "Traffic",
                    "password": "runrun123",
                },
                headers=self._json_headers(),
            )
            created = True
            self._send_request("GET", "/flowable-ui/idm-app/rest/admin/users?sort=idAsc&start=0")
        finally:
            if created:
                self._send_request(
                    "DELETE",
                    f"/flowable-ui/idm-app/rest/admin/users/{user_id}",
                    headers={"Origin": BASE_URL},
                    expected_statuses={200, 204, 404},
                )

    @task(2)
    def model_flow(self):
        version = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:8]
        model_id = None

        self._send_request(
            "GET",
            f"/flowable-ui/modeler/views/popup/process-create.html?version={version}",
        )
        try:
            resp = self._send_request(
                "POST",
                "/flowable-ui/modeler-app/rest/models",
                json={
                    "name": f"model-{suffix}",
                    "key": f"key{suffix}",
                    "description": "normal traffic model",
                    "modelType": 0,
                },
                headers=self._json_headers(),
            )
            model_id = resp.json()["id"]
            self._send_request(
                "GET",
                "/flowable-ui/modeler-app/rest/models?filter=processes&modelType=0&sort=modifiedDesc",
            )
            self._send_request("GET", "/flowable-ui/modeler/views/popup/model-delete.html")
            self._send_request(
                "GET",
                f"/flowable-ui/modeler-app/rest/models/{model_id}/parent-relations",
            )
        finally:
            if model_id:
                self._send_request(
                    "DELETE",
                    f"/flowable-ui/modeler-app/rest/models/{model_id}?cascade=false",
                    headers={"Origin": BASE_URL},
                    expected_statuses={200, 204, 404},
                )
