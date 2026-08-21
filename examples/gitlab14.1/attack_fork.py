import time
import os
import requests

BASE = "http://localhost:8080"
TOKEN = os.getenv("GITLAB_PRIVATE_TOKEN", "")  # api_token must be created in the UI

proj1 = "demo1" + str(int(time.time()))
proj1_fork = "fork_" + proj1
proj2 = "demo2" + str(int(time.time()))
proj2_fork = "fork_" + proj2

HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

def send(method, path, **kw):
    url = f"{BASE}{path}"
    last = None
    for _ in range(12):
        resp = requests.request(method, url, headers=HEADERS, **kw)
        # print(resp.text)
        rid = resp.headers.get("X-Request-Id")
        last = resp
        if resp.ok and rid:
            print(f"{method} {path} -> {resp.status_code} {rid}")
            return resp
        print(f"{method} {path} status={resp.status_code} RID: {rid}")
        time.sleep(5)
    return last

def create_project(name, path):
    for attempt in range(12):
        project_name = name if attempt == 0 else f"{name}_{attempt}_{int(time.time())}"
        project_path = path if attempt == 0 else f"{path}_{attempt}_{int(time.time())}"
        r = requests.post(
            f"{BASE}/api/v4/projects",
            headers=HEADERS,
            json={"name": project_name, "path": project_path},
        )
        rid = r.headers.get("X-Request-Id")
        if r.ok and rid:
            print(f"POST /api/v4/projects -> {r.status_code} {rid}")
            try:
                return int(r.json().get("id"))
            except Exception:
                raise RuntimeError("project creation response did not contain project id")
        print(f"POST /api/v4/projects status={r.status_code} RID: {rid}")
        time.sleep(5)
    raise RuntimeError("project creation did not produce a successful request id")

def get_custom_attrs(pid):
    resp = send("GET", f"/api/v4/projects/{pid}/custom_attributes")
    if not resp.ok:
        raise RuntimeError(f"custom attribute query failed: {resp.status_code}")

def delete_project(pid):
    resp = send("DELETE", f"/api/v4/projects/{pid}")
    if not resp.ok:
        raise RuntimeError(f"project delete failed: {resp.status_code}")

def fork_project_min(pid, name, path):
    # Keep only name and path
    api_path = f"/api/v4/projects/{pid}/fork"
    for attempt in range(12):
        fork_name = name if attempt == 0 else f"{name}_{attempt}_{int(time.time())}"
        fork_path = path if attempt == 0 else f"{path}_{attempt}_{int(time.time())}"
        resp = requests.post(
            f"{BASE}{api_path}",
            headers=HEADERS,
            json={"name": fork_name, "path": fork_path},
        )
        rid = resp.headers.get("X-Request-Id")
        if resp.ok and rid:
            print(f"POST {api_path} -> {resp.status_code} {rid}")
            return
        print(f"POST {api_path} status={resp.status_code} RID: {rid}")
        time.sleep(5)
    raise RuntimeError("project fork did not produce a successful request id")

def link_fork_relation(upstream_id, downstream_id):
    # Only create the relationship: mark downstream_id as a fork of upstream_id
    path = f"/api/v4/projects/{upstream_id}/fork/{downstream_id}"
    for _ in range(12):
        resp = requests.post(f"{BASE}{path}", headers=HEADERS)
        rid = resp.headers.get("X-Request-Id")
        if rid:
            print(f"POST {path} -> {resp.status_code} {rid}")
            return
        print(f"POST {path} status={resp.status_code} RID: {rid}")
        time.sleep(5)
    raise RuntimeError("fork relation trigger did not return X-Request-Id")

# Sequence 1: create -> inspect attributes -> delete -> inspect again
def sequence_1():
    pid = create_project("demo-uaf", "demo-uaf")
    if not pid:
        return
    get_custom_attrs(pid)
    delete_project(pid)
    time.sleep(2)
    get_custom_attrs(pid)

# Sequence 2: create two projects -> fork each with name/path only -> create fork relationship
def sequence_2():
    id1 = create_project(proj1, proj1+"proj-1")
    if not id1:
        print("no id1")
        return
    fork_project_min(id1, proj1_fork, proj1_fork+"proj-1-fork")

    id2 = create_project(proj2, proj2+"proj-2")
    if not id2:
        print("no id2")
        return
    fork_project_min(id2, proj2_fork, proj2_fork+"proj-2-fork")

    link_fork_relation(id1, id2)


if __name__ == "__main__":
    #sequence_1()
    sequence_2()
    pass
