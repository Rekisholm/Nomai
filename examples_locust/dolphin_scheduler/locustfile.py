# locust_test/dolphinscheduler/locustfile.py
import os
import json
import time
import uuid
from locust import HttpUser, task, between, events

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)
import requests

# ========== Configuration ==========
HOST = os.getenv("DOLPHIN_URL", "http://localhost:12345/dolphinscheduler")
USERNAME = os.getenv("DOLPHIN_USERNAME", "locust_user")
PASSWORD = os.getenv("DOLPHIN_PASSWORD", "locust123")
QUEUE_ID = int(os.getenv("DOLPHIN_QUEUE_ID", "1"))          # Default queue ID
TENANT_CODE = os.getenv("DOLPHIN_TENANT_CODE", "locust_tenant")  # Normal users cannot create tenants; use a preallocated one
WAIT_INSTANCE_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30")) # Maximum seconds to wait for instance completion

# ========== Helper functions ==========
def random_suffix() -> str:
    """Generate a 6-character random suffix"""
    return uuid.uuid4().hex[:6].lower()

def normalize_url_path(path: str) -> str:
    """
    Replace numeric/code IDs in URL paths with :id or :code for statistics aggregation
    Example: /projects/12345/process-definition/67890 -> /projects/:code/process-definition/:code
    """
    import re
    # Replace numeric IDs
    path = re.sub(r'/(\d+)(?=/|$)', r'/:id', path)
    # Replace project code, which is also numeric
    path = re.sub(r'/projects/:id', '/projects/:code', path)
    return path

class DolphinSchedulerUser(HttpUser):
    wait_time = between(1, 3)
    host = HOST

    def on_start(self):
        """On each virtual user startup: create a Session, log in, and initialize"""
        self.session = requests.Session()
        self._login()
        self.created_resources = {
            "tenant_id": None,
            "project_code": None,
            "definition_code": None,
            "tenant_code": None,
            "project_name": None,
            "workflow_name": None
        }

    def _send_request(self, method, url, name=None, **kwargs):
        """Send a request, manually record Locust statistics, and normalize URLs automatically"""
        full_url = f"{self.host}{url}" if url.startswith('/') else f"{self.host}/{url}"
        stat_name = name if name else normalize_url_path(url)
        start_time = time.time()
        try:
            resp = self.session.request(method, full_url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            # Determine business success; DolphinScheduler returns JSON with a success field
            success = True
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("success") is False:
                    success = False
                    exception = Exception(f"Business failure: {data.get('msg', 'unknown error')}")
            except:
                # For non-JSON responses or parse failures, use HTTP status code
                if resp.status_code >= 400:
                    success = False
                    exception = Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
                else:
                    success = True
                    exception = None
            if success:
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
                    exception=exception,
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
        """Log in to DolphinScheduler"""
        url = "/login"
        params = {"userName": USERNAME, "userPassword": PASSWORD}
        resp = self._send_request("POST", url, params=params)
        if resp.status_code != 200:
            raise Exception("Login failed")
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"Login failed: {data.get('msg')}")
        print(f"[OK] User {USERNAME} login succeeded")
    
    def logout(self):
        """Log out of DolphinScheduler"""
        url = "/signOut"
        self._send_request("POST", url)
        print(f"[OK] User {USERNAME} logged out")

    # ---------- API wrappers ----------
    def create_tenant(self, tenant_code: str, queue_id: int, description: str = ""):
        url = "/tenants"
        params = {"tenantCode": tenant_code, "queueId": queue_id, "description": description}
        resp = self._send_request("POST", url, params=params)
        data = resp.json()
        if data.get("success"):
            tenant_id = data.get("data", {}).get("id")
            return tenant_id
        else:
            raise Exception(f"Failed to create tenant: {data.get('msg')}")

    def delete_tenant(self, tenant_id: int):
        url = f"/tenants/{tenant_id}"
        self._send_request("DELETE", url)

    def create_project(self, project_name: str, description: str = ""):
        url = "/projects"
        params = {"projectName": project_name, "description": description}
        resp = self._send_request("POST", url, params=params)
        data = resp.json()
        if data.get("success"):
            project_code = data.get("data", {}).get("code")
            return project_code
        else:
            raise Exception(f"Failed to create project: {data.get('msg')}")

    def delete_project(self, project_code: int):
        url = f"/v2/projects/{project_code}"
        self._send_request("DELETE", url)

    def create_process_definition(self, project_code: int, name: str, locations: str,
                                   tenant_code: str, task_relation_json: str,
                                   task_definition_json: str, description: str = ""):
        url = f"/projects/{project_code}/process-definition"
        params = {
            "name": name,
            "locations": locations,
            "tenantCode": tenant_code,
            "taskRelationJson": task_relation_json,
            "taskDefinitionJson": task_definition_json,
            "description": description
        }
        resp = self._send_request("POST", url, params=params)
        data = resp.json()
        if data.get("success"):
            definition_code = data.get("data", {}).get("code")
            return definition_code
        else:
            raise Exception(f"Failed to create workflow: {data.get('msg')}")

    def release_process_definition(self, project_code: int, definition_code: int, release_state: str):
        url = f"/projects/{project_code}/process-definition/{definition_code}/release"
        params = {"releaseState": release_state}
        self._send_request("POST", url, params=params)

    def delete_process_definition(self, project_code: int, definition_code: int):
        url = f"/projects/{project_code}/process-definition/{definition_code}"
        self._send_request("DELETE", url)

    def start_process_instance(self, project_code: int, definition_code: int,
                                failure_strategy: str = "CONTINUE",
                                schedule_time: str = "",
                                warning_type: str = "NONE",
                                **kwargs):
        url = f"/projects/{project_code}/executors/start-process-instance"
        params = {
            "processDefinitionCode": definition_code,
            "failureStrategy": failure_strategy,
            "scheduleTime": schedule_time,
            "warningType": warning_type
        }
        # Optional parameters
        optional = ["processInstancePriority", "workerGroup", "warningGroupId", "environmentCode",
                    "startParams", "execType", "expectedParallelismNumber", "dryRun", "runMode",
                    "complementDependentMode", "taskDependType", "timeout", "startNodeList"]
        for key in optional:
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]
        resp = self._send_request("POST", url, params=params)
        data = resp.json()
        if data.get("success"):
            pass
        else:
            raise Exception(f"Failed to start workflow instance: {data.get('msg')}")

    # ---------- Main task: full lifecycle ----------
    @task(1)
    def full_lifecycle(self):
        """Each virtual user executes the full resource create/use/cleanup flow"""
        suffix = random_suffix()
        tenant_code = TENANT_CODE  # Normal users cannot create tenants; use a preallocated one
        project_name = f"locust_project_{suffix}"
        workflow_name = f"locust_workflow_{suffix}"

        print(f"[INFO] User started, suffix: {suffix}")

        # 1. Create project; normal users cannot create tenants, so skip tenant creation

        # 2. Create project
        try:
            project_code = self.create_project(project_name, "Locust test project")
            self.created_resources["project_code"] = project_code
            self.created_resources["project_name"] = project_name
            print(f"[OK] Project created successfully: {project_name} (Code:{project_code})")
        except Exception as e:
            print(f"[ERROR] Failed to create project: {e}")
            self._cleanup()  # Clean up created tenant
            return

        # 3. Create workflow definition with a simple Shell task
        task_code = int(time.time() * 1000) % 1000000000
        task_definition = [{
            "code": task_code,
            "name": "echo_task",
            "taskType": "SHELL",
            "taskParams": json.dumps({"rawScript": "echo 'test' > /tmp/test.txt"}),
            "workerGroup": "default",
            "timeoutFlag": "CLOSE"
        }]
        task_relation = [{
            "preTaskCode": 0,
            "postTaskCode": task_code,
            "name": "",
            "conditionType": "NONE"
        }]
        locations = json.dumps({"echo_task": {"x": 200, "y": 200}})

        try:
            definition_code = self.create_process_definition(
                project_code=project_code,
                name=workflow_name,
                locations=locations,
                tenant_code=tenant_code,
                task_relation_json=json.dumps(task_relation),
                task_definition_json=json.dumps(task_definition),
                description="Locust test workflow"
            )
            self.created_resources["definition_code"] = definition_code
            self.created_resources["workflow_name"] = workflow_name
            print(f"[OK] Workflow definition created successfully: {workflow_name} (Code:{definition_code})")
        except Exception as e:
            print(f"[ERROR] Failed to create workflow: {e}")
            self._cleanup()
            return

        # 4. Release workflow
        try:
            self.release_process_definition(project_code, definition_code, "ONLINE")
            print(f"[OK] Workflow released")
        except Exception as e:
            print(f"[ERROR] Release failed: {e}")
            self._cleanup()
            return

        # 5. Start workflow instance
        try:
            self.start_process_instance(
                project_code=project_code,
                definition_code=definition_code,
                failure_strategy="CONTINUE",
                schedule_time="",
                warning_type="NONE"
            )
        except Exception as e:
            print(f"[ERROR] Failed to start instance: {e}")
            self._cleanup()
            return

        # 6. Wait for workflow execution to complete, up to 3 seconds
        time.sleep(3)

        # 7. Clean up all resources
        self._cleanup()
        print(f"[OK] User {suffix} test iteration completed and cleanup finished")

    def _cleanup(self):
        """Clean up created resources in dependency order"""
        print("[INFO] Starting resource cleanup...")
        # Cleanup order: take workflow offline -> delete workflow definition -> delete project -> delete tenant
        if self.created_resources["definition_code"] and self.created_resources["project_code"]:
            try:
                print("  [INFO] Taking workflow offline...")
                self.release_process_definition(
                    self.created_resources["project_code"],
                    self.created_resources["definition_code"],
                    "OFFLINE"
                )
                print("  [OK] Workflow taken offline")
            except Exception as e:
                print(f"  [WARN] Failed to take offline; ignored: {e}")
            try:
                print("  [INFO] Deleting workflow definition...")
                self.delete_process_definition(
                    self.created_resources["project_code"],
                    self.created_resources["definition_code"]
                )
                print("  [OK] Workflow definition deleted")
            except Exception as e:
                print(f"  [WARN] Failed to delete workflow definition: {e}")

        if self.created_resources["project_code"]:
            try:
                print("  [INFO] Deleting project...")
                self.delete_project(self.created_resources["project_code"])
                print("  [OK] Project deleted")
            except Exception as e:
                print(f"  [WARN] Failed to delete project: {e}")

        # Note: normal users do not create/delete tenants, so skip tenant cleanup

        # Reset records
        self.created_resources = {k: None for k in self.created_resources}
        print("[INFO] Cleanup finished")

# Startup message
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== DolphinScheduler concurrent test started ===")
    print(f"Target URL: {HOST}")
    print(f"Login user: {USERNAME}")
    print(f"Queue ID: {QUEUE_ID}")
    print(f"Instance wait timeout: {WAIT_INSTANCE_TIMEOUT}s")

    # Use admin to create the normal user (locust_user) to ensure cross-user traffic isolation
    _ensure_locust_user()


def _ensure_locust_user():
    """Use admin to create locust_tenant and locust_user if they do not exist."""
    import requests as _req
    admin_session = _req.Session()
    resp = admin_session.post(f"{HOST}/login", params={"userName": "admin", "userPassword": "dolphinscheduler123"})
    if resp.status_code != 200 or not resp.json().get("success"):
        print(f"[WARN] admin login failed; unable to create locust_user: {resp.text[:200]}")
        return

    # Create tenant
    resp = admin_session.post(f"{HOST}/tenants",
                              params={"tenantCode": TENANT_CODE, "queueId": QUEUE_ID, "description": "locust tenant"})
    tenant_id = None
    if resp.status_code in (200, 201) and resp.json().get("success"):
        tenant_id = resp.json().get("data", {}).get("id")
        print(f"[OK] Tenant created: {TENANT_CODE} (ID:{tenant_id})")
    else:
        # Tenant may already exist; query to get the id
        resp2 = admin_session.get(f"{HOST}/tenants/list")
        for t in resp2.json().get("data", []):
            if t.get("tenantCode") == TENANT_CODE:
                tenant_id = t.get("id")
                break
        print(f"[INFO] Tenant already exists: {TENANT_CODE} (ID:{tenant_id})")

    if not tenant_id:
        print("[WARN] Unable to get tenant_id; skip user creation")
        return

    def can_login() -> bool:
        session = _req.Session()
        result = session.post(
            f"{HOST}/login",
            params={"userName": USERNAME, "userPassword": PASSWORD},
        )
        return result.status_code == 200 and result.json().get("success")

    # Check whether the user already exists to avoid login failure caused by duplicate creation
    resp = admin_session.get(f"{HOST}/users/list")
    existing = [u for u in resp.json().get("data", []) if u.get("userName") == USERNAME]
    if existing:
        if len(existing) == 1 and can_login():
            print(f"[INFO] User already exists and can log in: {USERNAME} (ID:{existing[0].get('id')})")
            return

        # Delete the old user and recreate it to ensure the password is correct
        for u in existing:
            uid = u.get("id")
            delete_resp = admin_session.post(f"{HOST}/users/delete", params={"id": uid})
            if delete_resp.status_code == 200 and delete_resp.json().get("success"):
                print(f"[INFO] Delete old user: {USERNAME} (ID:{uid})")
            else:
                print(
                    f"[WARN] Failed to delete old user: {USERNAME} (ID:{uid}) "
                    f"{delete_resp.text[:200]}"
                )

        resp = admin_session.get(f"{HOST}/users/list")
        existing = [u for u in resp.json().get("data", []) if u.get("userName") == USERNAME]
        if existing:
            print(f"[WARN] Old user still exists; skip duplicate creation: {USERNAME}")
            return

    # Create user
    resp = admin_session.post(f"{HOST}/users/create", params={
        "userName": USERNAME, "userPassword": PASSWORD,
        "tenantId": tenant_id, "queue": "default",
        "email": "locust@test.com", "phone": "13800000000", "state": 1,
    })
    if resp.status_code in (200, 201) and resp.json().get("success"):
        print(f"[OK] User created: {USERNAME}")
    else:
        print(f"[WARN] Failed to create user: {USERNAME} ({resp.json().get('msg', '')})")
