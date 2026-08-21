#!/usr/bin/env python3
import os
import uuid

import requests


BASE_URL = os.getenv("FLOWABLE_TARGET", "http://localhost:8080").rstrip("/")
USERNAME = os.getenv("FLOWABLE_USER", "admin")
PASSWORD = os.getenv("FLOWABLE_PASS", "test")


def request_id(resp):
    rid = resp.headers.get("X-Request-Id")
    return rid


def show_setup(step, resp):
    print(f"[setup] {step}: {resp.request.method} {resp.request.path_url} "
          f"status={resp.status_code} RID: {request_id(resp)}")


def show_core(step, resp):
    print(f"[core] Step {step}: {resp.request.method} {resp.request.path_url} "
          f"-> {resp.status_code} X-Request-Id: {request_id(resp)}")


def main():
    s = requests.Session()
    s.trust_env = False

    # Login is only a preparation step and is not included in the core attack chain in attack_rids.json.
    r = s.get(f"{BASE_URL}/flowable-ui/idm/views/login.html", timeout=20)
    show_setup("GET login page", r)

    r = s.post(
        f"{BASE_URL}/flowable-ui/app/authentication",
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
        timeout=20,
    )
    show_setup("POST login", r)

    # Preparation step: confirm the current account context; pure read requests are not included in attack_rids.json.
    r = s.get(f"{BASE_URL}/flowable-ui/idm-app/rest/account", timeout=20)
    show_setup("GET account", r)

    # Core attack chain 1: create the attack-marker task and write ACT_RU_TASK/ACT_HI_TASKINST.
    task_name = f"attack-task-{uuid.uuid4().hex[:8]}"
    r = s.post(
        f"{BASE_URL}/flowable-ui/app/rest/tasks",
        json={"name": task_name, "description": "attack provenance marker"},
        headers={"Content-Type": "application/json;charset=UTF-8", "Origin": BASE_URL},
        timeout=20,
    )
    show_core(1, r)
    task_id = r.json().get("id")

    if task_id:
        # Core attack chain 2: complete that task and read/update/delete the task row written in the previous step.
        r = s.put(
            f"{BASE_URL}/flowable-ui/app/rest/tasks/{task_id}/action/complete",
            headers={"Origin": BASE_URL},
            timeout=20,
        )
        show_core(2, r)


if __name__ == "__main__":
    main()
