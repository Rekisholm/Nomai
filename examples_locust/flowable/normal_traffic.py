"""Flowable UI normal traffic generator.

Run:
  conda run -n py312 python tests_locust/flowable/normal_traffic.py
"""
import os
import time
import uuid

import requests


BASE = os.getenv("FLOWABLE_BASE", "http://localhost:8080").rstrip("/")
USER = os.getenv("FLOWABLE_USER", "admin")
PWD = os.getenv("FLOWABLE_PASS", "test")
TIMEOUT = 10

s = requests.Session()
s.trust_env = False
s.headers.update({
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
})


def check(r, label, allowed=(200, 201, 204)):
    print(f"{label}: {r.status_code} ({len(r.content)}B)")
    if r.status_code not in allowed:
        raise RuntimeError(f"{label} failed: {r.status_code}\n{r.text[:500]}")
    return r


def get(path, label):
    return check(s.get(f"{BASE}{path}", timeout=TIMEOUT), label)


def post(path, label, payload=None, data=None):
    headers = {"Origin": BASE}
    if payload is not None:
        headers["Content-Type"] = "application/json;charset=UTF-8"
    r = s.post(f"{BASE}{path}", json=payload, data=data, headers=headers, timeout=TIMEOUT)
    return check(r, label)


def put(path, label):
    r = s.put(f"{BASE}{path}", headers={"Origin": BASE}, timeout=TIMEOUT)
    return check(r, label)


def delete(path, label):
    r = s.delete(f"{BASE}{path}", headers={"Origin": BASE}, timeout=TIMEOUT)
    return check(r, label)


def login():
    get("/flowable-ui/idm/views/login.html", "login page")
    r = s.post(
        f"{BASE}/flowable-ui/app/authentication",
        data={
            "j_username": USER,
            "j_password": PWD,
            "_spring_security_remember_me": "true",
            "submit": "Login",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE,
            "Referer": f"{BASE}/flowable-ui/idm/",
        },
        timeout=TIMEOUT,
    )
    check(r, "login")
    if "FLOWABLE_REMEMBER_ME" not in s.cookies.get_dict():
        raise RuntimeError("Login did not return FLOWABLE_REMEMBER_ME cookie")


def task_flow():
    get("/flowable-ui/workflow/views/tasks.html", "tasks page")
    suffix = uuid.uuid4().hex[:8]
    r = post(
        "/flowable-ui/app/rest/tasks",
        "create task",
        payload={"name": f"New task {suffix}", "description": "normal traffic task"},
    )
    task_id = r.json()["id"]
    put(f"/flowable-ui/app/rest/tasks/{task_id}/action/complete", "complete task")


def user_flow():
    get("/flowable-ui/idm-app/rest/account", "account")
    user_id = "u" + uuid.uuid4().hex[:8]
    post(
        "/flowable-ui/idm-app/rest/admin/users",
        "create user",
        payload={
            "id": user_id,
            "email": f"{user_id}@example.com",
            "firstName": "Normal",
            "lastName": "Traffic",
            "password": "runrun123",
        },
    )
    get("/flowable-ui/idm-app/rest/admin/users?sort=idAsc&start=0", "user list")
    delete(f"/flowable-ui/idm-app/rest/admin/users/{user_id}", "delete user")


def model_flow():
    version = int(time.time() * 1000)
    get(
        f"/flowable-ui/modeler/views/popup/process-create.html?version={version}",
        "process create page",
    )
    suffix = uuid.uuid4().hex[:8]
    r = post(
        "/flowable-ui/modeler-app/rest/models",
        "create model",
        payload={
            "name": f"model-{suffix}",
            "key": f"key{suffix}",
            "description": "normal traffic model",
            "modelType": 0,
        },
    )
    model_id = r.json()["id"]
    get(
        "/flowable-ui/modeler-app/rest/models?filter=processes&modelType=0&sort=modifiedDesc",
        "model list",
    )
    get("/flowable-ui/modeler/views/popup/model-delete.html", "model delete page")
    get(
        f"/flowable-ui/modeler-app/rest/models/{model_id}/parent-relations",
        "model parent relations",
    )
    delete(
        f"/flowable-ui/modeler-app/rest/models/{model_id}?cascade=false",
        "delete model",
    )


def main():
    login()
    time.sleep(0.2)
    task_flow()
    time.sleep(0.2)
    user_flow()
    time.sleep(0.2)
    model_flow()
    print("done")


if __name__ == "__main__":
    main()
