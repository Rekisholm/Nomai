import json
import random
import re
import time
import uuid

import psycopg2
import requests


BASE_URL = "http://localhost:5050"
USERNAME = "vulhub@example.com"
PASSWORD = "vulhub"
SERVER_NAME = "local-cve"
SERVER_GROUP_ID = "1"
DATABASE_OID = "16384"


session = requests.Session()
session.trust_env = False
csrf_token = None


def reset_pgadmin_server_rows():
    with psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="cve",
        user="postgres",
        password="postgres",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sharedserver WHERE name = %s", (SERVER_NAME,))
            cur.execute(
                "DELETE FROM server WHERE name = %s OR host = %s",
                (SERVER_NAME, "postgres"),
            )
            cur.execute(
                """
                INSERT INTO server(
                    user_id, servergroup_id, name, host, port, maintenance_db,
                    username, role, comment, save_password, use_ssh_tunnel,
                    connection_params
                )
                VALUES (1, 1, %s, %s, 5432, 'cve', 'postgres', NULL,
                        'Nomai provenance attack database', 0, 0, NULL)
                RETURNING id
                """,
                (SERVER_NAME, "postgres"),
            )
            return str(cur.fetchone()[0])


def request_id(resp):
    return resp.headers.get("X-Request-ID") or resp.headers.get("X-Request-Id")


def log_response(label, resp, capture=False, forced_rid=None):
    rid_label = "X-Request-Id" if capture else "RID"
    rid = forced_rid or request_id(resp)
    print(f"{label}: {resp.status_code} {rid_label}: {rid}")


def extract_csrf(html):
    patterns = [
        r'"csrfToken"\s*:\s*"([^"]+)"',
        r"'csrfToken'\s*:\s*'([^']+)'",
        r'name="csrf_token"\s+type="hidden"\s+value="([^"]+)"',
        r'name="csrf_token"\s+value="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def csrf_headers(json_request=True, rid=None):
    headers = {
        "X-pgA-CSRFToken": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
    }
    if json_request:
        headers["Content-Type"] = "application/json"
    if rid:
        headers["X-Request-ID"] = rid
    return headers


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def login():
    global csrf_token

    resp = session.get(f"{BASE_URL}/login")
    csrf_token = extract_csrf(resp.text)
    if not csrf_token:
        raise RuntimeError("Failed to extract login CSRF token")
    log_response("SETUP GET /login", resp, capture=False)

    resp = session.post(
        f"{BASE_URL}/authenticate/login",
        data={
            "csrf_token": csrf_token,
            "email": USERNAME,
            "password": PASSWORD,
        },
        allow_redirects=True,
    )
    log_response("SETUP POST /authenticate/login", resp, capture=False)

    resp = session.get(f"{BASE_URL}/browser/")
    csrf_token = extract_csrf(resp.text) or csrf_token
    log_response("SETUP GET /browser", resp, capture=False)


def existing_server_id():
    resp = session.get(
        f"{BASE_URL}/browser/server/nodes/{SERVER_GROUP_ID}",
        headers=csrf_headers(json_request=False),
    )
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return None

    for node in payload.get("result") or payload.get("data") or []:
        if node.get("label") == SERVER_NAME:
            return str(node.get("_id"))
    return None


def ensure_server():
    sid = existing_server_id()
    if sid:
        return sid

    server = {
        "name": SERVER_NAME,
        "host": "postgres",
        "port": 5432,
        "db": "cve",
        "username": "postgres",
        "role": "",
        "comment": "Nomai provenance attack database",
        "sslmode": "prefer",
        "connect_now": True,
        "password": "postgres",
        "db_password": "postgres",
        "save_password": False,
    }
    resp = session.post(
        f"{BASE_URL}/browser/server/obj/{SERVER_GROUP_ID}/",
        data=json.dumps(server),
        headers=csrf_headers(),
    )
    log_response("SETUP POST /browser/server/obj create server", resp, capture=False)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to create server: {resp.text[:300]}")
    return str(resp.json()["node"]["_id"])


def wait_query(trans_id, rid=None):
    final_resp = None
    for _ in range(60):
        time.sleep(0.2)
        final_resp = session.get(
            f"{BASE_URL}/sqleditor/poll/{trans_id}",
            headers=csrf_headers(json_request=False, rid=rid),
        )
        try:
            status = final_resp.json().get("data", {}).get("status")
        except json.JSONDecodeError:
            status = None
        if status not in {"Busy", "NotInitialised"}:
            return final_resp
    raise TimeoutError(f"SQL query did not finish: trans_id={trans_id}")


def run_sql(sql, label, sid, capture=False):
    rid = f"pgadmin-{uuid.uuid4()}" if capture else None
    trans_id = random.randint(100000, 9999999)

    resp = session.post(
        f"{BASE_URL}/browser/server/connect/{SERVER_GROUP_ID}/{sid}",
        data=json.dumps({"password": "postgres"}),
        headers=csrf_headers(rid=rid),
    )
    log_response(f"{label} connect", resp, capture=False)
    if resp.status_code != 200:
        raise RuntimeError(f"{label}: server connect failed: {resp.text[:300]}")

    resp = session.post(
        f"{BASE_URL}/sqleditor/initialize/sqleditor/{trans_id}/{SERVER_GROUP_ID}/{sid}/{DATABASE_OID}",
        data=json.dumps({"password": "postgres"}),
        headers=csrf_headers(rid=rid),
    )
    log_response(f"{label} initialize sqleditor", resp, capture=False)
    if resp.status_code != 200:
        raise RuntimeError(f"{label}: sqleditor init failed: {resp.text[:300]}")

    sql_with_rid = sql
    if rid:
        sql_with_rid = f"SELECT set_config('request_id', {sql_literal(rid)}, false);\n{sql}"

    resp = session.post(
        f"{BASE_URL}/sqleditor/query_tool/start/{trans_id}",
        data=json.dumps({"sql": sql_with_rid}),
        headers=csrf_headers(rid=rid),
    )
    log_response(label, resp, capture=capture, forced_rid=rid)
    if resp.status_code != 200:
        raise RuntimeError(f"{label}: query start failed: {resp.text[:300]}")

    final_resp = wait_query(trans_id, rid=rid)
    if final_resp.status_code != 200:
        raise RuntimeError(f"{label}: query poll failed: {final_resp.text[:300]}")
    try:
        payload = final_resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label}: poll response is not JSON") from exc
    if payload.get("success") != 1 or payload.get("data", {}).get("status") != "Success":
        raise RuntimeError(f"{label}: SQL did not succeed: {final_resp.text[:500]}")

    session.delete(
        f"{BASE_URL}/sqleditor/close/{trans_id}",
        headers=csrf_headers(json_request=False, rid=rid),
    )
    return rid, payload


def setup_database(sid, base_id):
    setup_sql = f"""
CREATE OR REPLACE FUNCTION public.nomai_copy_message(src_id integer, dst_id integer, suffix text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE src text;
BEGIN
  SELECT message INTO src FROM public.message WHERE message_id = src_id;
  INSERT INTO public.message(message_id, message)
  VALUES (dst_id, src || suffix)
  ON CONFLICT (message_id) DO UPDATE SET message = EXCLUDED.message;
  RETURN src || suffix;
END $$;
DELETE FROM public.message WHERE message_id BETWEEN {base_id} AND {base_id + 4};
"""
    run_sql(setup_sql, "SETUP prepare message chain", sid, capture=False)


def attack():
    sid = reset_pgadmin_server_rows()
    login()
    base_id = random.randint(200000, 900000)
    setup_database(sid, base_id)

    run_sql(
        f"INSERT INTO public.message(message_id, message) VALUES ({base_id}, 'payload:{base_id}')",
        "CORE[1] seed malicious payload row",
        sid,
        capture=True,
    )
    run_sql(
        (
            "SELECT message, "
            f"public.nomai_copy_message(message_id, {base_id + 1}, '|stage2') AS copied "
            f"FROM public.message WHERE message_id = {base_id}"
        ),
        "CORE[2] read seed and derive stage2 row",
        sid,
        capture=True,
    )
    run_sql(
        (
            "SELECT message, "
            f"public.nomai_copy_message(message_id, {base_id + 2}, '|stage3') AS copied "
            f"FROM public.message WHERE message_id = {base_id + 1}"
        ),
        "CORE[3] read stage2 and derive stage3 row",
        sid,
        capture=True,
    )
    run_sql(
        f"SELECT message_id, message FROM public.message WHERE message_id = {base_id + 2}",
        "CORE[4] final trigger reads stage3 payload",
        sid,
        capture=True,
    )


if __name__ == "__main__":
    attack()
