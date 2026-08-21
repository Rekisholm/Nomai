#!/usr/bin/env python3
import os
import random
import string

import requests


BASE_URL = os.getenv("OFBIZ_TARGET", "http://localhost:8080")
USERNAME = os.getenv("OFBIZ_USERNAME", "admin")
PASSWORD = os.getenv("OFBIZ_PASSWORD", "ofbiz")


def random_suffix(length=8):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def show(step, resp, capture=True):
    rid = resp.headers.get("X-Request-Id")
    rid_label = "X-Request-Id" if capture else "RID"
    if capture:
        print(f"[+] Step {step}: {resp.request.method} {resp.request.path_url} "
              f"-> {resp.status_code} {rid_label}: {rid}")
    else:
        print(f"[+] Step {step}: {resp.request.method} {resp.request.path_url} "
              f"status={resp.status_code} {rid_label}: {rid}")


def main():
    s = requests.Session()
    s.trust_env = False

    r = s.get(f"{BASE_URL}/projectmgr/control/login", timeout=30)
    show(1, r, capture=False)

    # OFBiz is a heavy Java EE app; under 50-user Locust load the login response
    # body can stall. Use stream=True so we capture headers (RID + Set-Cookie)
    # without waiting for the full body.
    r = s.post(
        f"{BASE_URL}/projectmgr/control/login",
        data={
            "USERNAME": USERNAME,
            "PASSWORD": PASSWORD,
            "JavaScriptEnabled": "N",
        },
        allow_redirects=False,
        timeout=120,
        stream=True,
    )
    show(2, r)
    r.close()

    project_id = f"ATKPRJ{random_suffix()}"
    marker = f"ATTACK_PROJECT_{project_id}"
    r = s.post(
        f"{BASE_URL}/projectmgr/control/createProject",
        data={
            "workEffortId": project_id,
            "projectId": project_id,
            "workEffortTypeId": "PROJECT",
            "workEffortName": marker,
            "description": f"attack provenance marker {marker}",
            "currentStatusId": "_NA_",
            "scopeEnumId": "WES_PRIVATE",
            "priority": "5",
            "organizationPartyId": "Company",
        },
        allow_redirects=False,
        timeout=120,
        stream=True,
    )
    show(3, r)
    r.close()
    print(f"[+] Attack project marker: {marker}")
    print(f"[+] Attack project id: {project_id}")

    r = s.get(
        f"{BASE_URL}/projectmgr/control/projectView",
        params={"projectId": project_id},
        timeout=120,
        stream=True,
    )
    show(4, r)
    r.close()


if __name__ == "__main__":
    main()
