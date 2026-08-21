# locust_test/gitlab8/locustfile.py
import os
import re
import time
import random
import string
import base64
from locust import HttpUser, task, between, events

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)
import requests
from util import html_2_json

# ========== Configuration ==========
HOST = os.getenv("GITLAB_HOST", "http://localhost")
PORT = int(os.getenv("GITLAB_PORT", "8080"))
BASE_URL = f"{HOST}:{PORT}"
# Path to the uploaded tar.gz file; prepare it in advance
TAR_FILE_PATH = os.getenv("GITLAB_IMPORT_FILE", "./test.tar.gz")
# Whether registration is allowed; set False and use an existing user if self-registration is disabled
ALLOW_REGISTER = os.getenv("GITLAB_ALLOW_REGISTER", "true").lower() == "true"
# Preset user, used when registration is not allowed
DEFAULT_USERNAME = os.getenv("GITLAB_USERNAME", "root")
DEFAULT_PASSWORD = os.getenv("GITLAB_PASSWORD", "vulhub123456")

# ========== Helper functions ==========
def random_str(n=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def normalize_url_path(path):
    """Replace numeric IDs in URL paths with :id for statistics aggregation"""
    return re.sub(r'/(\d+)(?=/|$)', r'/:id', path)

class GitLabUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        """On user startup: create a Session, get CSRF token, and register or log in"""
        self.session = requests.Session()
        self.authenticity_token = None
        self.user_name = None          # Username used in URL paths
        self.password = None
        self.created_project_id = None
        self.created_group_name = None

        # 1. Visit the login page to get the token
        resp = self._send_request("GET", "/users/sign_in")
        if resp.status_code != 200:
            raise Exception("Unable to visit the login page")
        self.authenticity_token = html_2_json(resp.text).get("csrf_token", "")
        if not self.authenticity_token:
            raise Exception("Unable to extract authenticity_token")

        if ALLOW_REGISTER:
            # Register a new user
            self._register_user()
        else:
            # Log in with the preset user
            self._login_user()

        # After registration/login succeeds, get the token again; it may have been refreshed
        # Visit the home page once to ensure the session is valid
        resp = self._send_request("GET", "/")
        token = html_2_json(resp.text).get("csrf_token", "")
        if token:
            self.authenticity_token = token

    def _register_user(self):
        """Register a new user"""
        username = f"user_{random_str(6)}"
        name = f"Test User {random_str(4)}"
        email = f"{username}@example.com"
        password = "Passw0rd!"
        data = {
            "authenticity_token": self.authenticity_token,
            "new_user[name]": name,
            "new_user[username]": username,
            "new_user[email]": email,
            "new_user[password]": password,
        }
        resp = self._send_request("POST", "/users", data=data, allow_redirects=False)
        if resp.status_code not in (302, 200):
            raise Exception(f"Registration failed: {resp.status_code}")
        self.user_name = username
        self.password = password
        print(f"Registration succeeded: {username}")

    def _login_user(self):
        """Log in with the preset user"""
        data = {
            "authenticity_token": self.authenticity_token,
            "user[login]": DEFAULT_USERNAME,
            "user[password]": DEFAULT_PASSWORD,
            "user[remember_me]": "0",
        }
        resp = self._send_request("POST", "/users/sign_in", data=data, allow_redirects=False)
        if resp.status_code != 302:
            raise Exception(f"Login failed: {resp.status_code}")
        self.user_name = DEFAULT_USERNAME
        self.password = DEFAULT_PASSWORD
        print(f"Login succeeded: {DEFAULT_USERNAME}")

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

    # ---------- Business operations ----------
    def create_project_by_import(self, project_name):
        """Create a project by importing a tar.gz file and return the project path (user_name/project_name)"""
        # 1. Get namespace_id
        resp = self._send_request("GET", "/projects/new")
        namespace_id = html_2_json(resp.text).get("namespace_id", "")
        if not namespace_id:
            namespace_id = "1"

        # 2. Visit the import page (GET)
        params = {"namespace_id": namespace_id, "path": project_name}
        resp = self._send_request("GET", "/import/gitlab_project/new", params=params)
        # A new token may be obtained on this page, but it usually does not change

        # 3. Run import (POST)
        if not os.path.exists(TAR_FILE_PATH):
            # Create a dummy file if it does not exist
            with open(TAR_FILE_PATH, "wb") as f:
                f.write(b"dummy content")
        with open(TAR_FILE_PATH, "rb") as f:
            file_content = f.read()
        file_b64 = base64.b64encode(file_content).decode()
        data = {
            "authenticity_token": self.authenticity_token,
            "namespace_id": namespace_id,
            "path": project_name,
        }
        files = {"file": (f"{project_name}.tar.gz", file_content, "application/gzip")}
        # allow_redirects=False: POST returns 302; capture the write RID
        # Note: actual DB writes for project import are executed asynchronously by Sidekiq; the POST itself only enqueues the job
        resp = self._send_request("POST", "/import/gitlab_project", data=data, files=files, allow_redirects=False)
        if resp.status_code not in (200, 302):
            raise Exception(f"Import failed: {resp.status_code}")
        time.sleep(1)  # Wait for import completion
        return f"{self.user_name}/{project_name}"

    def delete_project(self, project_path):
        """Delete a project; project_path format: user_name/project_name"""
        # 1. Visit the project home page to get the token needed for deletion
        resp = self._send_request("GET", f"/{project_path}", name = "/:project_path")
        token = html_2_json(resp.text).get("csrf_token", "")
        if not token:
            token = self.authenticity_token
        # 2. Send the delete request (POST with _method=delete)
        data = {"_method": "delete", "authenticity_token": token}
        self._send_request("POST", f"/{project_path}", name = "/:project_path", data=data, allow_redirects=False)

    def star_project(self, project_path):
        """Star a project; requires the X-CSRF-Token header"""
        # Visit the project home page first to get the CSRF token
        resp = self._send_request("GET", f"/{project_path}", name = "/:project_path")
        csrf_token = html_2_json(resp.text).get("csrf_token", "")
        if not csrf_token:
            csrf_token = self.authenticity_token
        headers = {"X-CSRF-Token": csrf_token}
        # allow_redirects=False: toggle_star returns 302, and the write happens in the POST.
        # The POST RID must be captured, not the redirected landing-page RID, or the write cannot be attributed to this request.
        self._send_request("POST", f"/{project_path}/toggle_star", name = "/:project_path/toggle_star", headers=headers, allow_redirects=False)

    def add_email(self, email):
        """Add an alternate email"""
        # Visit the email settings page to get the token
        resp = self._send_request("GET", "/profile/emails")
        token = html_2_json(resp.text).get("csrf_token", "")
        if not token:
            token = self.authenticity_token
        data = {"authenticity_token": token, "email[email]": email}
        # allow_redirects=False: POST returns 302; capture the write RID
        self._send_request("POST", "/profile/emails", data=data, allow_redirects=False)

    def create_issue(self, project_path, title):
        """Create an issue"""
        # Get the token from the new issue page
        resp = self._send_request("GET", f"/{project_path}/issues/new", name = "/:project_path/issues/new")
        token = html_2_json(resp.text).get("csrf_token", "")
        if not token:
            token = self.authenticity_token
        data = {
            "authenticity_token": token,
            "issue[title]": title,
            "issue[description]": "",
            "issue[confidential]": "0",
            "issue[label_ids][]": "",
            "issue[due_date]": "",
            "issue[lock_version]": "0",
        }
        self._send_request("POST", f"/{project_path}/issues", name = "/:project_path/issues", data=data, allow_redirects=False)

    def create_group(self, group_name):
        """Create a group and return the group path"""
        # Get the token from the group creation page
        resp = self._send_request("GET", "/groups/new")
        token = html_2_json(resp.text).get("csrf_token", "")
        if not token:
            token = self.authenticity_token
        data = {
            "authenticity_token": token,
            "group[path]": group_name,
            "group[description]": "Test group",
            "group[visibility_level]": "0",  # private
        }
        resp = self._send_request("POST", "/groups", data=data, allow_redirects=False)
        if resp.status_code not in (200, 302):
            raise Exception(f"Failed to create group: {resp.status_code}")
        return group_name

    def delete_group(self, group_name):
        """Delete group"""
        # Visit the group edit page to get the token
        resp = self._send_request("GET", f"/groups/{group_name}/edit", name = "/groups/:group_name/edit")
        token = html_2_json(resp.text).get("csrf_token", "")
        if not token:
            token = self.authenticity_token
        data = {"_method": "delete", "authenticity_token": token}
        self._send_request("POST", f"/{group_name}", name = "/:group_name",data=data, allow_redirects=False)

    # ---------- Main task: full lifecycle ----------
    @task(1)
    def full_lifecycle(self):
        """Execute a complete user behavior sequence: create project -> operations -> cleanup"""
        # 1. Create the main project through import
        proj_name = f"locust-proj-{random_str(6)}"
        project_path = self.create_project_by_import(proj_name)
        print(f"Project created successfully: {project_path}")
        time.sleep(0.5)

        # 2. Visit project pages
        pages = [
            f"/{project_path}",
            f"/{project_path}/activity",
            f"/{project_path}/pipelines",
            f"/{project_path}/issues",
            f"/{project_path}/merge_requests",
            f"/{project_path}/wikis/home",
            f"/{project_path}/snippets",
        ]

        page_api_names = [
            "/:user_name/:project_path",
            "/:user_name/:project_path/activity",
            "/:user_name/:project_path/pipelines",
            "/:user_name/:project_path/issues",
            "/:user_name/:project_path/wikis/home",
            "/:user_name/:project_path/snippets",
        ]
        for page, api_name in zip(pages, page_api_names):
            self._send_request("GET", page, name = api_name)
            time.sleep(0.2)

        # 3. Star project
        self.star_project(project_path)
        time.sleep(0.3)

        # 4. Add an alternate email
        email = f"alt-{random_str(5)}@{random_str(5)}.com"
        self.add_email(email)
        time.sleep(0.3)

        # 5. Create an issue
        issue_title = f"Issue-{random_str(5)}"
        self.create_issue(project_path, issue_title)
        time.sleep(0.3)

        # 6. Create and delete a temporary project to test deletion
        temp_proj = f"temp-{random_str(5)}"
        temp_path = self.create_project_by_import(temp_proj)
        time.sleep(0.5)
        self.delete_project(temp_path)
        print(f"Temporary project deleted: {temp_path}")
        time.sleep(0.3)

        # 7. Create and delete a group
        group_name = f"group-{random_str(5)}"
        self.create_group(group_name)
        time.sleep(0.5)
        self.delete_group(group_name)
        print(f"Group deleted: {group_name}")
        time.sleep(0.3)

        # 8. Finally delete the main project
        self.delete_project(project_path)
        print(f"Main project deleted: {project_path}")

# Startup message
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== GitLab 8.13 concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Import file: {TAR_FILE_PATH}")
    print(f"Allow registration: {ALLOW_REGISTER}")
    if not ALLOW_REGISTER:
        print(f"Using preset user: {DEFAULT_USERNAME}")
