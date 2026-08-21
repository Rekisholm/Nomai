# locust_test/kanboard/locustfile.py
from collections import deque
import os
import re
import json
import time
import threading
import uuid
from locust import HttpUser, task, between, events

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)
import requests
from bs4 import BeautifulSoup

# ========== Configuration ==========
HOST = os.getenv("KANBOARD_HOST", "http://localhost")
PORT = int(os.getenv("KANBOARD_PORT", "8080"))
BASE_URL = f"{HOST}:{PORT}"
USER_PREFIX = os.getenv("KANBOARD_USER_PREFIX", "user")
USER_PASSWORD = os.getenv("KANBOARD_USER_PASSWORD", "123456")
USER_COUNT = int(os.getenv("KANBOARD_USER_COUNT", "10"))


def build_account_pool():
    users = os.getenv("KANBOARD_USERS")
    if not users:
        if USER_COUNT <= 0:
            raise ValueError("KANBOARD_USER_COUNT must be positive")
        return [(f"{USER_PREFIX}{idx}", USER_PASSWORD) for idx in range(1, USER_COUNT + 1)]

    accounts = []
    for raw_item in users.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            username, password = item.split(":", 1)
        else:
            username, password = item, USER_PASSWORD
        accounts.append((username.strip(), password.strip()))
    if not accounts:
        raise ValueError("KANBOARD_USERS is set but no valid accounts were parsed")
    return accounts


ACCOUNT_POOL = build_account_pool()
AVAILABLE_ACCOUNTS = deque(ACCOUNT_POOL)
ACCOUNT_LOCK = threading.Lock()


def acquire_account():
    with ACCOUNT_LOCK:
        if not AVAILABLE_ACCOUNTS:
            raise RuntimeError(
                f"Not enough Kanboard accounts for current Locust concurrency; "
                f"available={len(ACCOUNT_POOL)}. Run init_users.py with a larger "
                f"KANBOARD_USER_COUNT or lower LOCUST_USERS."
            )
        return AVAILABLE_ACCOUNTS.popleft()


def release_account(account):
    if not account:
        return
    with ACCOUNT_LOCK:
        AVAILABLE_ACCOUNTS.append(account)

# ========== Helper functions ==========
def random_suffix() -> str:
    """Generate a 6-character alphanumeric random suffix"""
    return uuid.uuid4().hex[:6].lower()

def normalize_url_path(path: str) -> str:
    """Replace numeric IDs in URLs with :id for statistics aggregation"""
    return re.sub(r'/(\d+)(?=/|$)', r'/:id', path)

def extract_csrf_token_from_html(html: str) -> str:
    """
    Extract CSRF token from HTML using meta tags, inputs, JavaScript, and other patterns
    """
    soup = BeautifulSoup(html, "html.parser")
    # 1. Find <meta name="csrf-token" content="...">
    meta = soup.find("meta", attrs={"name": re.compile(r"csrf", re.I)})
    if meta and meta.get("content"):
        return meta["content"]
    # 2. Find <input name="csrf_token" value="...">
    inp = soup.find("input", {"name": "csrf_token"})
    if inp and inp.get("value"):
        return inp["value"]
    # 3. Findhidden fields whose value contains a token
    for inp in soup.find_all("input", {"type": "hidden"}):
        if inp.get("name") and "token" in inp["name"].lower() and inp.get("value"):
            return inp["value"]
    # 4. Match from JS variables or HTML text
    match = re.search(r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)', html)
    if match:
        return match.group(1)
    match = re.search(r'csrf_token=([a-zA-Z0-9_\-]+)', html)
    if match:
        return match.group(1)
    return None

class KanboardUser(HttpUser):
    wait_time = between(1, 2)
    host = BASE_URL

    def on_start(self):
        """On each virtual user startup: create a Session, log in, and initialize"""
        self.session = requests.Session()
        self.created_project_id = None
        self.account = None
        try:
            self.account = acquire_account()
            self.username, self.password = self.account
            self._login()
        except Exception:
            release_account(self.account)
            self.account = None
            raise

    def on_stop(self):
        """Return the account when the virtual user stops so Locust can reuse it during rescheduling."""
        release_account(self.account)
        self.account = None

    def _send_request(self, method, url, name=None, **kwargs):
        """
        Send a request, manually record Locust statistics, and normalize URLs automatically.
        Supports files and data parameters.
        """
        full_url = f"{BASE_URL}{url}" if url.startswith('/') else f"{BASE_URL}/{url}"
        stat_name = name if name else normalize_url_path(url)
        start_time = time.time()
        try:
            resp = self.session.request(method, full_url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            # Determine business success; HTTP 2xx is considered success without deep JSON parsing
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

    def _login(self):
        """Log in to Kanboard"""
        # 1. Get the CSRF token from the login page
        resp = self._send_request("GET", "/login")
        csrf_token = extract_csrf_token_from_html(resp.text)
        if not csrf_token:
            raise Exception("Unable to get CSRF token from the login page")

        # 2. Submit login
        login_url = "/?controller=AuthController&action=check"
        data = {
            "csrf_token": csrf_token,
            "username": self.username,
            "password": self.password,
            "remember_me": "1",
        }
        resp = self._send_request("POST", login_url, data=data)
        if resp.status_code != 200:
            raise Exception(f"Login failed: {resp.status_code}")

        print(f"User {self.username} Login succeeded")

    def _get_projects_list(self):
        """Get project list; for demonstration only, not mandatory"""
        url = "/projects"
        resp = self._send_request("GET", url)
        if resp.status_code == 200:
            # Parse project list (optional)
            soup = BeautifulSoup(resp.text, "html.parser")
            div = soup.find("div", class_="js-select-dropdown-autocomplete")
            if div and div.get("data-params"):
                try:
                    params = json.loads(div["data-params"])
                    items = params.get("items", {})
                    print(f"[INFO] Current project count: {len(items)}")
                except:
                    pass
        return resp

    def _create_project(self):
        """Create a project and return the new project ID"""
        # 1. Get the project creation page and extract CSRF token using AJAX headers
        headers = {"X-Requested-With": "XMLHttpRequest"}
        resp = self._send_request("GET", "/project/create/personal", headers=headers)
        csrf_token = extract_csrf_token_from_html(resp.text)
        if not csrf_token:
            raise Exception("Unable to get CSRF token from the project creation page")

        # 2. Submit project creation (multipart/form-data)
        proj_name = f"locust_proj_{random_suffix()}"
        create_url = "/?controller=ProjectCreationController&action=save"
        files = {
            "csrf_token": (None, csrf_token),
            "is_private": (None, ""),
            "name": (None, proj_name),
            "identifier": (None, ""),
            "task_limit": (None, "100"),
            "src_project_id": (None, "0"),
            "projectPermissionModel": (None, "1"),
            "projectRoleModel": (None, "1"),
            "categoryModel": (None, "1"),
            "tagDuplicationModel": (None, "1"),
            "actionModel": (None, "1"),
            "customFilterModel": (None, "1"),
        }
        headers = {"X-Requested-With": "XMLHttpRequest"}
        resp = self._send_request("POST", create_url, files=files, headers=headers)
        if resp.status_code != 200:
            raise Exception(f"Failed to create project: {resp.status_code}")

        # Extract project ID from the X-Ajax-Redirect response header
        redirect_url = resp.headers.get("X-Ajax-Redirect")
        if not redirect_url:
            raise Exception("Project creation response is missing the X-Ajax-Redirect header")

        # Redirect format is similar to /project/123
        match = re.search(r'/project/(\d+)', redirect_url)
        if not match:
            raise Exception(f"Unable to extract project ID from redirect URL: {redirect_url}")
        project_id = match.group(1)
        print(f"[OK] Project created successfully: {proj_name} (ID: {project_id})")
        return project_id

    def _delete_project(self, project_id):
        """Delete the specified project"""
        # 1. Visit the delete confirmation page and get CSRF token
        remove_page_url = f"/project/{project_id}/remove"
        resp = self._send_request("GET", remove_page_url)
        # Extract the csrf_token parameter from the page, inside the link
        match = re.search(r'csrf_token=([a-zA-Z0-9_\-]+)', resp.text)
        if not match:
            raise Exception("Unable to extract CSRF token from the delete page")
        csrf_token = match.group(1)

        # 2. Execute deletion (GET request with query parameters)
        delete_url = f"/?controller=ProjectStatusController&action=remove&project_id={project_id}&csrf_token={csrf_token}"
        resp = self._send_request("GET", delete_url, name="/?controller=ProjectStatusController&action=remove&project_id=:id&csrf_token=:token")
        if resp.status_code != 200:
            raise Exception(f"Failed to delete project: {resp.status_code}")
        print(f"[OK] Project deleted successfully: ID {project_id}")

    # ---------- Main task ----------
    @task(1)
    def full_lifecycle(self):
        """Complete business flow: get project list -> create project -> delete project"""
        # 1. Get project list (optional, simulates user browsing)
        self._get_projects_list()
        time.sleep(0.5)

        # 2. Create project
        try:
            project_id = self._create_project()
            self.created_project_id = project_id
        except Exception as e:
            print(f"[ERROR] Failed to create project: {e}")
            return  # Stop this iteration

        # 3. Delete project to ensure cleanup
        try:
            self._delete_project(project_id)
        except Exception as e:
            print(f"[ERROR] Failed to delete project: {e}, project ID may remain {project_id}")
        finally:
            self.created_project_id = None

# Startup message
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    _ = (environment, kwargs)
    print("=== Kanboard concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Account pool: {len(ACCOUNT_POOL)} users ({ACCOUNT_POOL[0][0]}..{ACCOUNT_POOL[-1][0]})")
