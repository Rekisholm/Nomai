#!/usr/bin/env python3
"""Initialize the Admidio 5.0.5 database through the installer AJAX flow.
Usage: python init_db.py [TARGET_URL]
After the scene starts for the first time, the DB is empty; run this script to complete installation.
"""
import re
import json
import sys
import requests

TARGET = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3100"
ORG_SHORT = "GBV"
ORG_LONG = "GBV"
ORG_EMAIL = "admin@gbv.example"
ORG_TZ = "Europe/Vienna"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "Aa!123456"

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0"})

def csrf_of(text):
    m = re.search(r'adm_csrf_token[^>]*value="([^"]+)"', text)
    return m.group(1) if m else None

def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None

# Step 1: GET create_organization form -> initialize the session and form object
r = s.get(f"{TARGET}/install/installation.php?step=create_organization")
csrf = csrf_of(r.text)
print(f"[1] org form: {r.status_code} csrf={csrf}")
if not csrf:
    txt = re.sub(r'<[^>]+>', ' ', r.text)
    print("FAIL - no csrf. Page text:", re.sub(r'\s+', ' ', txt)[:300])
    sys.exit(1)

# Step 2: POST create_organization &mode=check -> AJAX validation returns JSON
data = {
    "adm_csrf_token": csrf,
    "adm_organization_shortname": ORG_SHORT,
    "adm_organization_longname": ORG_LONG,
    "adm_organization_email": ORG_EMAIL,
    "adm_organization_timezone": ORG_TZ,
}
r = s.post(f"{TARGET}/install/installation.php?step=create_organization&mode=check", data=data)
j = parse_json(r.text)
print(f"[2] org check: {j}")
if not j or j.get("status") != "success":
    print(f"    raw: {r.text[:300]}")
    sys.exit(1)

# Step 3: GET create_administrator form
r = s.get(j["url"])
csrf = csrf_of(r.text)
print(f"[3] admin form: {r.status_code} csrf={csrf}")
if not csrf:
    txt = re.sub(r'<[^>]+>', ' ', r.text)
    print("FAIL - no csrf. Page text:", re.sub(r'\s+', ' ', txt)[:300])
    sys.exit(1)

# Step 4: POST create_administrator &mode=check
data = {
    "adm_csrf_token": csrf,
    "adm_user_last_name": "Admin",
    "adm_user_first_name": "User",
    "adm_user_email": ORG_EMAIL,
    "adm_user_login": ADMIN_USER,
    "adm_user_password": ADMIN_PASSWORD,
    "adm_user_password_confirm": ADMIN_PASSWORD,
}
r = s.post(f"{TARGET}/install/installation.php?step=create_administrator&mode=check", data=data)
j = parse_json(r.text)
print(f"[4] admin check: {j}")
if not j or j.get("status") != "success":
    print(f"    raw: {r.text[:400]}")
    sys.exit(1)

# Step 5: GET start_installation -> run the actual installation: create tables, organization, and admin user
r = s.get(f"{TARGET}/install/installation.php?step=start_installation", allow_redirects=True)
print(f"[5] start_installation: {r.status_code} url={r.url}")
txt = re.sub(r'<[^>]+>', ' ', re.sub(r'<(script|style)[^>]*>.*?</\1>', '', r.text, flags=re.S))
txt = re.sub(r'\s+', ' ', txt).strip()
print(f"    text: {txt[:300]}")

# Step 6: Verify
r = s.get(f"{TARGET}/modules/overview.php", allow_redirects=True)
csrf = csrf_of(r.text)
print(f"[6] overview: {r.status_code} url={r.url} csrf={csrf}")
if "overview" in r.url and csrf:
    print("SUCCESS: Admidio initialization completed")
else:
    txt = re.sub(r'<[^>]+>', ' ', r.text)
    txt_compact = re.sub(r'\s+', ' ', txt)[:200]
    print(f"WARN: overview is not ready: {txt_compact}")
