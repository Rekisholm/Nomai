#!/usr/bin/env python3
import os
import re
import sys

import requests
from bs4 import BeautifulSoup


HOST = os.getenv("KANBOARD_HOST", "http://localhost")
PORT = int(os.getenv("KANBOARD_PORT", "8080"))
BASE_URL = f"{HOST}:{PORT}"

ADMIN_USERNAME = os.getenv("KANBOARD_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("KANBOARD_ADMIN_PASSWORD", "admin")
USER_PREFIX = os.getenv("KANBOARD_USER_PREFIX", "user")
USER_PASSWORD = os.getenv("KANBOARD_USER_PASSWORD", "123456")
USER_COUNT = int(os.getenv("KANBOARD_USER_COUNT", "10"))
EMAIL_DOMAIN = os.getenv("KANBOARD_USER_EMAIL_DOMAIN", "example.com")
VERIFY_USERS = os.getenv("KANBOARD_VERIFY_USERS", "1").lower() not in {"0", "false", "no"}
INIT_VIA_DB = os.getenv("KANBOARD_INIT_VIA_DB", "1").lower() not in {"0", "false", "no"}
DB_DSN = os.getenv(
    "KANBOARD_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=kanboard user=kanboard password=kanboard-secret",
)
DEFAULT_PASSWORD_HASH = "$2y$10$DEeNIxqDbk5QxS2fS9oO0eqkzBQUvbLwGHJJeI4j9mfyCNFMJUxBu"
PASSWORD_HASH = os.getenv("KANBOARD_USER_PASSWORD_HASH", DEFAULT_PASSWORD_HASH)


def extract_csrf_token(html):
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"name": re.compile(r"csrf", re.I)})
    if meta and meta.get("content"):
        return meta["content"]

    inp = soup.find("input", {"name": "csrf_token"})
    if inp and inp.get("value"):
        return inp["value"]

    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        value = inp.get("value")
        if "token" in name.lower() and value:
            return value

    match = re.search(r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)', html)
    if match:
        return match.group(1)

    match = re.search(r'csrf_token=([a-zA-Z0-9_\-]+)', html)
    if match:
        return match.group(1)

    return None


def stripped_texts(html):
    soup = BeautifulSoup(html, "html.parser")
    return {text.strip() for text in soup.stripped_strings if text.strip()}


def login(session):
    resp = session.get(f"{BASE_URL}/login", timeout=10)
    resp.raise_for_status()
    csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        raise RuntimeError("failed to extract login csrf_token")

    data = {
        "csrf_token": csrf_token,
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD,
        "remember_me": "1",
    }
    resp = session.post(
        f"{BASE_URL}/?controller=AuthController&action=check",
        data=data,
        timeout=10,
    )
    resp.raise_for_status()

    users_page = session.get(f"{BASE_URL}/users", timeout=10)
    users_page.raise_for_status()
    if ADMIN_USERNAME not in stripped_texts(users_page.text):
        raise RuntimeError("admin login did not reach /users page")


def login_as_user(username):
    session = requests.Session()
    resp = session.get(f"{BASE_URL}/login", timeout=10)
    resp.raise_for_status()
    csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        raise RuntimeError(f"failed to extract login csrf_token for {username}")

    resp = session.post(
        f"{BASE_URL}/?controller=AuthController&action=check",
        data={
            "csrf_token": csrf_token,
            "username": username,
            "password": USER_PASSWORD,
            "remember_me": "1",
        },
        timeout=10,
    )
    resp.raise_for_status()

    projects_page = session.get(f"{BASE_URL}/projects", timeout=10)
    projects_page.raise_for_status()
    if "/login" in projects_page.url or 'name="username"' in projects_page.text:
        raise RuntimeError(f"login verification failed for {username}")


def fetch_users_page(session):
    resp = session.get(f"{BASE_URL}/users", timeout=10)
    resp.raise_for_status()
    return resp.text


def fetch_user_ids(session):
    users_page = fetch_users_page(session)
    user_ids = {}
    ids = sorted({int(match.group(1)) for match in re.finditer(r"/user/(\d+)/edit", users_page)})

    for user_id in ids:
        resp = session.get(f"{BASE_URL}/user/{user_id}/edit", timeout=10)
        if resp.status_code >= 400:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        username_input = soup.find("input", {"name": "username"})
        if username_input and username_input.get("value"):
            user_ids[username_input["value"]] = user_id

    return user_ids


def get_user_creation_csrf(session):
    resp = session.get(
        f"{BASE_URL}/?controller=UserCreationController&action=show",
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=10,
    )
    resp.raise_for_status()
    csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        raise RuntimeError("failed to extract user creation csrf_token")
    return csrf_token


def create_user(session, username):
    csrf_token = get_user_creation_csrf(session)
    files = {
        "csrf_token": (None, csrf_token),
        "username": (None, username),
        "name": (None, username),
        "email": (None, f"{username}@{EMAIL_DOMAIN}"),
        "password": (None, USER_PASSWORD),
        "confirmation": (None, USER_PASSWORD),
        "role": (None, "app-user"),
        "timezone": (None, ""),
        "language": (None, ""),
        "filter": (None, ""),
        "project_id": (None, "0"),
    }
    resp = session.post(
        f"{BASE_URL}/?controller=UserCreationController&action=save",
        files=files,
        headers={"X-Requested-With": "XMLHttpRequest"},
        allow_redirects=False,
        timeout=10,
    )
    resp.raise_for_status()

    user_ids = fetch_user_ids(session)
    if username not in user_ids:
        raise RuntimeError(f"user creation did not create visible account: {username}")
    return user_ids[username]


def reset_password(session, user_id, username):
    resp = session.get(f"{BASE_URL}/user/{user_id}/password", timeout=10)
    resp.raise_for_status()
    csrf_token = extract_csrf_token(resp.text)
    if not csrf_token:
        raise RuntimeError(f"failed to extract password csrf_token for {username}")

    data = {
        "csrf_token": csrf_token,
        "id": str(user_id),
        "current_password": ADMIN_PASSWORD,
        "password": USER_PASSWORD,
        "confirmation": USER_PASSWORD,
    }
    resp = session.post(
        f"{BASE_URL}/?controller=UserCredentialController&action=savePassword&user_id={user_id}",
        data=data,
        allow_redirects=False,
        timeout=10,
    )
    resp.raise_for_status()


def ensure_users_via_db():
    if USER_PASSWORD != "123456" and PASSWORD_HASH == DEFAULT_PASSWORD_HASH:
        raise RuntimeError(
            "KANBOARD_USER_PASSWORD_HASH must be set when using a non-default password"
        )

    import psycopg2

    created = 0
    updated = 0
    with psycopg2.connect(DB_DSN) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            for idx in range(1, USER_COUNT + 1):
                username = f"{USER_PREFIX}{idx}"
                cursor.execute(
                    """
                    INSERT INTO users
                        (username, password, name, email, role, is_active)
                    VALUES
                        (%s, %s, %s, %s, 'app-user', true)
                    ON CONFLICT (username) DO UPDATE
                    SET password = EXCLUDED.password,
                        name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        role = 'app-user',
                        is_active = true,
                        nb_failed_login = 0,
                        lock_expiration_date = 0
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        username,
                        PASSWORD_HASH,
                        username,
                        f"{username}@{EMAIL_DOMAIN}",
                    ),
                )
                inserted = cursor.fetchone()[0]
                if inserted:
                    created += 1
                    print(f"created user via db: {username}")
                else:
                    updated += 1
                    print(f"updated user via db: {username}")

    if VERIFY_USERS:
        for idx in range(1, USER_COUNT + 1):
            username = f"{USER_PREFIX}{idx}"
            login_as_user(username)
            print(f"verified login: {username}")

    print(
        f"done base_url={BASE_URL} users={USER_PREFIX}1..{USER_PREFIX}{USER_COUNT} "
        f"created={created} updated={updated} init=database"
    )


def main():
    if USER_COUNT <= 0:
        raise RuntimeError("KANBOARD_USER_COUNT must be positive")

    if INIT_VIA_DB:
        ensure_users_via_db()
        return

    session = requests.Session()
    login(session)
    user_ids = fetch_user_ids(session)

    created = 0
    skipped = 0
    for idx in range(1, USER_COUNT + 1):
        username = f"{USER_PREFIX}{idx}"
        user_id = user_ids.get(username)
        if user_id:
            print(f"skip existing user: {username}")
            skipped += 1
        else:
            user_id = create_user(session, username)
            user_ids[username] = user_id
            print(f"created user: {username}")
            created += 1

        reset_password(session, user_id, username)
        print(f"reset password: {username}")

    if VERIFY_USERS:
        for idx in range(1, USER_COUNT + 1):
            username = f"{USER_PREFIX}{idx}"
            login_as_user(username)
            print(f"verified login: {username}")

    print(
        f"done base_url={BASE_URL} users={USER_PREFIX}1..{USER_PREFIX}{USER_COUNT} "
        f"created={created} skipped={skipped}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"init_users failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
