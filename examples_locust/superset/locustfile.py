# locust_test/superset/locustfile.py
import os
import re
import time
import random
import string
import requests
from util import html_2_json
from loguru import logger
from locust import HttpUser, task, between, events

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)

# ========== Configuration ==========
HOST = os.getenv("SUPERSET_HOST", "http://localhost")
PORT = int(os.getenv("SUPERSET_PORT", "8088"))
BASE_URL = f"{HOST}:{PORT}"
USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
PASSWORD = os.getenv("SUPERSET_PASSWORD", "vulhub")
# Database connection URI; adjust for the target environment
DB_URI = os.getenv("SUPERSET_DB_URI", "postgresql+psycopg2://superset:superset@postgres:5432/superset")

# ========== Helper functions ==========
def random_str(n=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def extract_csrf_token(html):
    json_data = html_2_json(html)
    csrf_token = json_data.get("csrf_token")
    return csrf_token

def normalize_url_path(path):
    """Replace numeric IDs in URL paths with :id for statistics aggregation"""
    return re.sub(r'/(\d+)(?=/|$)', r'/:id', path)

class SupersetUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def _send_request(self, method, url, name=None, **kwargs):
        """Send a request, manually record Locust statistics, and normalize URLs automatically"""
        full_url = f"{BASE_URL}{url}"
        stat_name = name if name else normalize_url_path(url)
        start_time = time.time()
        try:
            resp = self.session.request(method, full_url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            if resp.status_code < 400:
                events.request.fire(
                    request_type=method.upper(),
                    name=stat_name,
                    response_time=response_time,
                    response_length=len(resp.content),
                    context={},
                    exception=None,
                )
            else:
                events.request.fire(
                    request_type=method.upper(),
                    name=stat_name,
                    response_time=response_time,
                    response_length=len(resp.content),
                    context={},
                    exception=Exception(f"HTTP {resp.status_code}: {resp.text[:100]}"),
                )
            return resp
        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=0,
                context={},
                exception=e,
            )
            raise

    def on_start(self):
        """Each virtual user only logs in at startup and does not pre-create attack-related objects."""
        self.session = requests.Session()
        self.session.trust_env = False
        self.csrf_token = None
        self.database_id = None

        self._login()

    def _login(self):
        """Log in and get the CSRF token without creating a dashboard"""
        resp = self._send_request("GET", "/login")
        self.csrf_token = extract_csrf_token(resp.text)
        if not self.csrf_token:
            raise Exception("Unable to extract csrf_token from the login page")

        login_data = {
            "csrf_token": self.csrf_token,
            "username": USERNAME,
            "password": PASSWORD,
        }
        resp = self._send_request("POST", "/login", data=login_data, allow_redirects=True)
        if resp.status_code not in (200, 302, 308):
            raise Exception(f"Login failed: {resp.status_code}")

        # Visit the welcome page and refresh csrf_token
        resp = self._send_request("GET", "/superset/welcome")
        self.csrf_token = extract_csrf_token(resp.text)
        # Get current user information
        self._send_request("GET", "/api/v1/me")

    def _add_database(self):
        """Create a normal-only database connection and return database_id."""
        url = "/api/v1/database/"
        headers = {
            "Origin": BASE_URL,
            "X-CSRFToken": self.csrf_token,
        }
        db_name = f"Normal-Background-{random_str(8)}"
        payload = {
            "engine": "postgresql",
            "configuration_method": "sqlalchemy_form",
            "database_name": db_name,
            "sqlalchemy_uri": DB_URI,
            "expose_in_sqllab": True,
            "allow_dml": True,
        }
        resp = self._send_request("POST", url, headers=headers, json=payload)
        if resp.status_code == 201:
            db_id = resp.json().get("id")
            print(f"normal database added successfully, ID: {db_id}, name: {db_name}")
            return db_id
        else:
            print(f"Failed to add database: {resp.status_code}")
            return None

    def _execute_readonly_sql(self, database_id):
        """Call the SQL API to run a read-only probe without modifying the Superset metadata database."""
        if not database_id:
            return
        url = "/superset/sql_json/"
        payload = {
            "client_id": "",
            "database_id": database_id,
            "json": True,
            "runAsync": False,
            "schema": None,
            "sql": "SELECT 1 AS normal_probe",
            "sql_editor_id": "1",
            "tab": f"NormalQuery-{random_str(4)}",
            "tmp_table_name": "",
            "select_as_cta": False,
            "ctas_method": "TABLE",
            "queryLimit": 1000,
            "expand_data": True,
        }
        headers = {"X-CSRFToken": self.csrf_token}
        self._send_request("POST", url, headers=headers, json=payload)

    # ---------- Main task: background read traffic + independent normal database + read-only SQL ----------
    @task(1)
    def full_flow(self):
        """Generate normal background traffic that does not touch dashboard_permalink/key_value."""
        self._send_request("GET", "/api/v1/database/")
        self._send_request("GET", "/api/v1/dashboard/_info?q=(keys:!(permissions))")

        database_id = self._add_database()
        if database_id:
            self._execute_readonly_sql(database_id)

        time.sleep(0.3)

# Startup message
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== Superset concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Login user: {USERNAME}")
