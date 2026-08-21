# locust_test/gitlab14/locustfile.py
import os
import random
import string
import time
import requests
import re
from locust import HttpUser, task, between, events

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)

# ========== Configuration ==========
HOST = os.getenv("GITLAB_HOST", "http://localhost")
PORT = int(os.getenv("GITLAB_PORT", "8080"))
BASE_URL = f"{HOST}:{PORT}"
# GitLab private token; optional, omit if the instance does not require authentication.
PRIVATE_TOKEN = os.getenv("GITLAB_PRIVATE_TOKEN", "")

# API path constants based on GitLab 14 REST API
API_V4 = "/api/v4"

# Helper functions
def random_str(n=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def extract_project_id(resp):
    """Extract project ID from the project creation response"""
    try:
        return resp.json().get("id")
    except Exception:
        return None

class GitLabUser(HttpUser):
    """
    Simulate a GitLab user executing a complete project lifecycle:
    create project -> view statistics/properties -> star/unstar -> boards -> milestones -> labels -> delete project
    """
    wait_time = between(0.5, 1.5)   # User operation interval
    host = BASE_URL

    def on_start(self):
        """On each virtual user startup: create an independent Session and set authentication headers"""
        self.session = requests.Session()
        if PRIVATE_TOKEN:
            self.session.headers.update({"Authorization": f"Bearer {PRIVATE_TOKEN}"})
        print(f"[User startup] GitLab session created, token: {'set' if PRIVATE_TOKEN else 'not set'}")

    
    
    def _send_request(self, method, url, name=None, **kwargs):
        """
        Send HTTP requests and manually record them in Locust statistics
        """
        def _normalize_path(path):
            """Replace numeric IDs in paths with :id for statistics aggregation"""
            return re.sub(r'/(\d+)(?=/|$)', r'/:id', path)
        
        full_url = f"{BASE_URL}{url}"
        start_time = time.time()
        stat_name = name if name is not None else _normalize_path(url)
        try:
            resp = self.session.request(method, full_url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            # Record success/failure
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
                name=url,
                response_time=response_time,
                response_length=0,
                context={},
                exception=e,
            )
            raise

    # ---------- Business API wrappers ----------
    def create_project(self, name, path):
        """Create a project and return the project ID"""
        url = f"{API_V4}/projects"
        payload = {"name": name, "path": path}
        resp = self._send_request("POST", url, json=payload)
        if resp.status_code == 201:
            return extract_project_id(resp)
        return None

    def get_project_statistics(self, project_id):
        """Get project statistics"""
        url = f"{API_V4}/projects/{project_id}/statistics"
        self._send_request("GET", url)

    def star_project(self, project_id):
        """Star project"""
        url = f"{API_V4}/projects/{project_id}/star"
        self._send_request("POST", url)

    def unstar_project(self, project_id):
        """Unstar project"""
        url = f"{API_V4}/projects/{project_id}/unstar"
        self._send_request("POST", url)

    def get_starred_projects(self, user_id):
        """Get the user's starred project list; usually user_id is the current user, can be set to 1 or parsed from the token"""
        url = f"{API_V4}/users/{user_id}/starred_projects"
        self._send_request("GET", url)

    # Board operations
    def list_boards(self, project_id):
        url = f"{API_V4}/projects/{project_id}/boards"
        self._send_request("GET", url)

    def create_board(self, project_id, name):
        url = f"{API_V4}/projects/{project_id}/boards"
        payload = {"name": name}
        resp = self._send_request("POST", url, json=payload)
        if resp.status_code == 201:
            return resp.json().get("id")
        return None

    def get_board(self, project_id, board_id):
        url = f"{API_V4}/projects/{project_id}/boards/{board_id}"
        self._send_request("GET", url)

    def delete_board(self, project_id, board_id):
        url = f"{API_V4}/projects/{project_id}/boards/{board_id}"
        self._send_request("DELETE", url)

    # Milestone operations
    def list_milestones(self, project_id):
        url = f"{API_V4}/projects/{project_id}/milestones"
        self._send_request("GET", url)

    def create_milestone(self, project_id, title):
        url = f"{API_V4}/projects/{project_id}/milestones"
        payload = {"title": title}
        resp = self._send_request("POST", url, json=payload)
        if resp.status_code == 201:
            return resp.json().get("id")
        return None

    def get_milestone(self, project_id, milestone_id):
        url = f"{API_V4}/projects/{project_id}/milestones/{milestone_id}"
        self._send_request("GET", url)

    def update_milestone(self, project_id, milestone_id, new_title):
        url = f"{API_V4}/projects/{project_id}/milestones/{milestone_id}"
        payload = {"title": new_title}
        self._send_request("PUT", url, json=payload)

    def delete_milestone(self, project_id, milestone_id):
        url = f"{API_V4}/projects/{project_id}/milestones/{milestone_id}"
        self._send_request("DELETE", url)

    # Label operations
    def list_labels(self, project_id):
        url = f"{API_V4}/projects/{project_id}/labels"
        self._send_request("GET", url)

    def create_label(self, project_id, name, color="#5843AD"):
        url = f"{API_V4}/projects/{project_id}/labels"
        payload = {"name": name, "color": color}
        resp = self._send_request("POST", url, json=payload)
        if resp.status_code == 201:
            return resp.json().get("id")
        return None

    def get_label(self, project_id, label_id):
        url = f"{API_V4}/projects/{project_id}/labels/{label_id}"
        self._send_request("GET", url)

    def subscribe_label(self, project_id, label_id):
        url = f"{API_V4}/projects/{project_id}/labels/{label_id}/subscribe"
        self._send_request("POST", url)

    def unsubscribe_label(self, project_id, label_id):
        url = f"{API_V4}/projects/{project_id}/labels/{label_id}/unsubscribe"
        self._send_request("POST", url)

    def delete_label(self, project_id, label_id):
        url = f"{API_V4}/projects/{project_id}/labels/{label_id}"
        self._send_request("DELETE", url)

    def delete_project(self, project_id):
        """Delete project"""
        url = f"{API_V4}/projects/{project_id}"
        self._send_request("DELETE", url)

    # ---------- Main task: complete business flow ----------
    @task(1)
    def full_lifecycle(self):
        """
        Execute a complete GitLab project lifecycle:
        create -> operate -> delete
        """
        # 1. Create project with a random name
        proj_name = f"locust-proj-{random_str(6)}"
        proj_path = f"locust-proj-{random_str(6)}"
        project_id = self.create_project(proj_name, proj_path)
        if not project_id:
            print(f"Project creation failed; skipping subsequent steps")
            return
        print(f"Project created successfully: ID={project_id}, Name={proj_name}")
        time.sleep(0.5)

        # 2. Get project statistics and custom attributes
        self.get_project_statistics(project_id)
        time.sleep(0.3)

        # 3. Star/unstar project
        self.star_project(project_id)
        time.sleep(0.3)
        self.unstar_project(project_id)
        time.sleep(0.3)
        # Get the current user's starred list; assumes current user ID is 1, but it can be parsed from the token
        self.get_starred_projects(1)
        time.sleep(0.3)

        # 4. Board operations
        self.list_boards(project_id)
        board_name = f"board-{random_str(5)}"
        board_id = self.create_board(project_id, board_name)
        if board_id:
            self.get_board(project_id, board_id)
            time.sleep(0.3)
            self.delete_board(project_id, board_id)
        time.sleep(0.3)

        # 5. Milestone operations
        self.list_milestones(project_id)
        milestone_title = f"ms-{random_str(5)}"
        milestone_id = self.create_milestone(project_id, milestone_title)
        if milestone_id:
            self.get_milestone(project_id, milestone_id)
            self.update_milestone(project_id, milestone_id, f"{milestone_title}-updated")
            # Optional: get issues/merge requests associated with the milestone; omitted here because it requires creating an issue
            self.delete_milestone(project_id, milestone_id)
        time.sleep(0.3)

        # 6. Label operations
        self.list_labels(project_id)
        label_name = f"label-{random_str(4)}"
        label_id = self.create_label(project_id, label_name)
        if label_id:
            self.get_label(project_id, label_id)
            self.subscribe_label(project_id, label_id)
            time.sleep(0.2)
            self.unsubscribe_label(project_id, label_id)
            self.delete_label(project_id, label_id)
        time.sleep(0.3)

        # 7. Delete project to clean up resources
        self.delete_project(project_id)
        print(f"Project deleted: {project_id}")

    # Optional: test one operation separately; uncomment to enable
    # @task(1)
    # def just_create_and_delete(self):
    #     proj_name = f"quick-{random_str(6)}"
    #     proj_path = f"quick-{random_str(6)}"
    #     pid = self.create_project(proj_name, proj_path)
    #     if pid:
    #         time.sleep(0.5)
    #         self.delete_project(pid)

# Startup output
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== GitLab 14 concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Authentication mode: {'Private Token' if PRIVATE_TOKEN else 'No authentication (depends on Cookie)'}")
