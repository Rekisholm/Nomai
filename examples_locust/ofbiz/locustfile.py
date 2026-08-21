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
HOST = os.getenv("OFBIZ_HOST", "http://localhost")
PORT = int(os.getenv("OFBIZ_PORT", "8080"))
BASE_URL = f"{HOST}:{PORT}"
USERNAME = os.getenv("OFBIZ_USERNAME", "admin")
PASSWORD = os.getenv("OFBIZ_PASSWORD", "ofbiz")

# Use a safe Groovy expression by default and only verify that the ProgramExport API is available.
# Set OFBIZ_ENABLE_ATTACK_PAYLOAD=true to reproduce command-execution scenarios.
ENABLE_ATTACK_PAYLOAD = os.getenv("OFBIZ_ENABLE_ATTACK_PAYLOAD", "false").lower() in ("1", "true", "yes")
UPLOAD_DIR = os.getenv("OFBIZ_UPLOAD_DIR", "/usr/src/apache-ofbiz/runtime/uploads")


# ========== Helper functions ==========
def random_str(length=8):
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def normalize_url_path(path):
    path = re.sub(r"externalLoginKey=[^&]+", "externalLoginKey=:key", path)
    path = re.sub(r"/(\d+)(?=/|$)", "/:id", path)
    return path


def extract_external_login_key(html):
    match = re.search(r"externalLoginKey=([^\"&<]+)", html)
    return match.group(1) if match else None


def extract_note_id_for_marker(html, marker):
    marker_index = html.find(marker)
    if marker_index < 0:
        return None
    fragment = html[marker_index : marker_index + 1500]
    match = re.search(r'<input name="noteId" value="(\d+)"', fragment)
    return match.group(1) if match else None


class OfbizUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.external_login_key = None
        self._login_all_apps()

    def _send_request(self, method, url, name=None, expected_statuses=None, **kwargs):
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

    def _login_app(self, app):
        login_path = f"/{app}/control/login"
        self._send_request("GET", login_path)
        resp = self._send_request(
            "POST",
            login_path,
            data={
                "USERNAME": USERNAME,
                "PASSWORD": PASSWORD,
                "JavaScriptEnabled": "N",
            },
        )
        if resp.status_code != 200 or "username was empty" in resp.text or "password was empty" in resp.text:
            raise Exception(f"OFBiz {app} Login failed: {resp.status_code}")
        return resp

    def _login_all_apps(self):
        """Different OFBiz webapps use JSESSIONID values with different paths, so log in to each at startup."""
        self._login_app("myportal")
        main_resp = self._send_request("GET", "/myportal/control/main")
        self.external_login_key = extract_external_login_key(main_resp.text)
        if not self.external_login_key:
            raise Exception("Unable to extract externalLoginKey from myportal/control/main")

        self._login_app("content")
        self._send_request("GET", "/content/control/main")

        self._login_app("webtools")
        self._send_request("GET", "/webtools/control/main")

        self._login_app("projectmgr")
        self._send_request("GET", "/projectmgr/control/main")

    def _create_note(self):
        marker = f"locust_{random_str(10)}"
        resp = self._send_request(
            "POST",
            "/myportal/control/createSystemInfoNote",
            data={
                "noteParty": USERNAME,
                "moreInfoUrl": marker,
                "noteInfo": f"Locust note {marker}",
            },
        )
        if resp.status_code != 200:
            return None
        return marker

    def _delete_note_by_marker(self, marker):
        resp = self._send_request("GET", "/myportal/control/main")
        note_id = extract_note_id_for_marker(resp.text, marker)
        if not note_id:
            return

        self._send_request(
            "POST",
            "/myportal/control/deleteSystemInfoNote",
            data={
                "portalPageId": "10000",
                "VIEW_SIZE_1": "20",
                "noteId": note_id,
                "VIEW_INDEX_1": "0",
            },
        )

    def _create_data_resource(self, resource_id):
        payload = {
            "dataResourceId": resource_id,
            "dataResourceTypeId": "LOCAL_FILE",
            "dataTemplateTypeId": "",
            "dataCategoryId": "",
            "dataSourceId": "",
            "dataResourceName": f"Locust {resource_id}",
            "localeString": "",
            "mimeTypeId": "text/plain",
            "characterSetId": "UTF-8",
            "objectInfo": "",
            "surveyId": "",
            "surveyResponseId": "",
            "relatedDetailId": "",
            "isPublic": "Y",
            "createdDate_i18n": "",
            "createdDate": "",
            "createdByUserLogin": "",
            "lastModifiedDate_i18n": "",
            "lastModifiedDate": "",
            "lastModifiedByUserLogin": "",
        }
        return self._send_request("POST", "/content/control/createDataResource", data=payload)

    def _upload_file(self):
        resource_id = f"LOC{random_str(10).upper()}"
        create_resp = self._create_data_resource(resource_id)
        if create_resp.status_code != 200:
            return

        file_name = f"{resource_id}.txt"
        file_content = io.BytesIO(f"Locust upload {resource_id}".encode("utf-8"))
        self._send_request(
            "POST",
            "/content/control/uploadImage",
            data={
                "dataResourceId": resource_id,
                "dataResourceTypeId": "LOCAL_FILE",
                "objectInfo": "",
                "submitButton": "Upload",
            },
            files={"imageData": (file_name, file_content, "text/plain")},
            headers={"Referer": f"{BASE_URL}/content/control/uploadImage"},
        )

    def _program_export(self):
        if ENABLE_ATTACK_PAYLOAD:
            file_name = f"attack_{random_str(10)}"
            cmd = f"touch {UPLOAD_DIR}/{file_name}"
            groovy_program = f"throw new Exception('{cmd}'.execute().text);"
        else:
            groovy_program = "println 'locust program export check'"

        self._send_request(
            "POST",
            "/webtools/control/main/ProgramExport",
            files={"groovyProgram": (None, groovy_program)},
        )

    @task(3)
    def note_lifecycle(self):
        marker = self._create_note()
        time.sleep(0.2)
        if marker:
            self._delete_note_by_marker(marker)

    @task(2)
    def content_upload_flow(self):
        self._upload_file()

    @task(1)
    def webtools_program_export(self):
        self._program_export()

    @task(1)
    def browse_pages(self):
        self._send_request("GET", "/myportal/control/main")
        time.sleep(0.2)
        self._send_request("GET", "/content/control/main")
        time.sleep(0.2)
        self._send_request("GET", "/webtools/control/main")

    @task(4)
    def project_lifecycle(self):
        """Normal project-management business flow: create project -> view -> browse list.

        Key purpose: write normal business data to the work_effort table to prevent the pruning algorithm
        from misclassifying it as a read-only configuration table. Project entity tables carry real business data and should not be pruned.
        """
        project_id = f"LOCPRJ{random_str(8).upper()}"
        project_name = f"Locust Project {random_str(6)}"
        # allow_redirects=False: createProject returns 302, and the DB write happens in the initial POST.
        # The POST RID must be captured, not the redirected landing-page RID, or the write cannot be attributed to this request.
        self._send_request(
            "POST",
            "/projectmgr/control/createProject",
            data={
                "workEffortId": project_id,
                "projectId": project_id,
                "workEffortTypeId": "PROJECT",
                "workEffortName": project_name,
                "description": f"locust normal project {project_id}",
                "currentStatusId": "_NA_",
                "scopeEnumId": "WES_PRIVATE",
                "priority": "5",
                "organizationPartyId": "Company",
            },
            expected_statuses={200, 302},
            allow_redirects=False,
        )
        time.sleep(0.2)
        self._send_request(
            "GET",
            "/projectmgr/control/projectView",
            params={"projectId": project_id},
            expected_statuses={200, 302},
        )
        time.sleep(0.2)
        self._send_request(
            "GET",
            "/projectmgr/control/findProject",
            expected_statuses={200, 302},
        )


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== OFBiz concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Login user: {USERNAME}")
    print(f"Attack payload: {'enabled' if ENABLE_ATTACK_PAYLOAD else 'disabled'}")
