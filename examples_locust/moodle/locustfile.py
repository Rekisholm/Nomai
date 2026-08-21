import csv
import json
import os
import random
import re
import time
from itertools import cycle
from pathlib import Path

import requests
from locust import HttpUser, between, events, task
from gevent.lock import Semaphore

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("MOODLE_LOCUST_CONFIG", ROOT / "moodle_locust_config.json"))
REQUEST_LOGGER = RequestLogger(ROOT / "request_log")
REQUEST_LOGGER.install(events)


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "base_url": os.getenv("MOODLE_BASE_URL", "http://10.0.0.252:8081"),
        "course_id": int(os.getenv("MOODLE_COURSE_ID", "0")),
        "page_activity_id": int(os.getenv("MOODLE_PAGE_ACTIVITY_ID", "0")),
        "forum_activity_id": int(os.getenv("MOODLE_FORUM_ACTIVITY_ID", "0")),
        "forum_discussion_id": int(os.getenv("MOODLE_FORUM_DISCUSSION_ID", "0")),
        "forum_reply_id": int(os.getenv("MOODLE_FORUM_REPLY_ID", "0")),
        "users_csv": os.getenv("MOODLE_USERS_CSV", "locust_users.csv"),
    }


CONFIG = load_config()
BASE_URL = os.getenv("MOODLE_BASE_URL", CONFIG["base_url"]).rstrip("/")
TIMEOUT = int(os.getenv("MOODLE_TIMEOUT", "30"))
USERS_CSV = Path(CONFIG.get("users_csv", "locust_users.csv"))
if not USERS_CSV.is_absolute():
    USERS_CSV = ROOT / USERS_CSV


def load_users():
    with USERS_CSV.open(newline="", encoding="utf-8") as handle:
        rows = [(row[0], row[1]) for row in csv.reader(handle) if len(row) >= 2 and row[0]]
    if not rows:
        raise RuntimeError(f"no users found in {USERS_CSV}")
    return rows


USERS_LIST = load_users()
USERS = cycle(USERS_LIST)
USER_LOCK = Semaphore()


def next_user():
    with USER_LOCK:
        return next(USERS)


def extract(patterns, text, default=""):
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1)
    return default


def normalize_path(path):
    path = re.sub(r"([?&](id|d|reply|course|discussion|userid|parent)=)\d+", r"\1{id}", path)
    path = re.sub(r"/(\d+)(?=/|$)", "/{id}", path)
    return path


class MoodleUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Firefox/115.0",
        })
        self.username, self.password = next_user()
        self.sesskey = ""
        self.userid = "0"
        self.login()

    def _send_request(self, method, path, name=None, expected_statuses=None, **kwargs):
        url = f"{BASE_URL}{path}"
        stat_name = name or normalize_path(path)
        expected_statuses = expected_statuses or set(range(200, 400))
        start_time = time.time()

        try:
            kwargs.setdefault("timeout", TIMEOUT)
            resp = self.session.request(method, url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, path)
            response_time = (time.time() - start_time) * 1000
            exception = None
            if resp.status_code not in expected_statuses:
                exception = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=len(resp.content),
                context={},
                exception=exception,
            )
            return resp
        except Exception as exc:
            response_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=0,
                context={},
                exception=exc,
            )
            raise

    def login(self):
        self._send_request("GET", "/", name="Frontpage not logged")
        login_page = self._send_request("GET", "/login/index.php", name="View login page")
        logintoken = extract([r'name="logintoken"\s+value="([^"]+)"', r'value="([^"]+)"\s+name="logintoken"'], login_page.text)
        if not logintoken:
            raise RuntimeError("failed to extract Moodle logintoken")

        resp = self._send_request(
            "POST",
            "/login/index.php",
            name="Login",
            data={
                "anchor": "",
                "logintoken": logintoken,
                "username": self.username,
                "password": self.password,
            },
            allow_redirects=False,
            expected_statuses={303},
        )
        location = resp.headers.get("Location", "")
        if location:
            redirect_path = location.replace(BASE_URL, "")
            if redirect_path.startswith("http://") or redirect_path.startswith("https://"):
                redirect_resp = self.session.get(location, timeout=TIMEOUT)
                REQUEST_LOGGER.write_response(redirect_resp, time.time(), "GET", location)
            else:
                self._send_request("GET", redirect_path, name="Login testsession")

        frontpage = self._send_request("GET", "/my/", name="Frontpage logged")
        if "logout.php" not in frontpage.text:
            raise RuntimeError(f"Moodle login failed for {self.username}")

        self.sesskey = extract([r"sesskey=([^\"&]+)", r'"sesskey":"([^"]+)"'], frontpage.text)

    def _ids(self):
        return (
            CONFIG["course_id"],
            CONFIG["page_activity_id"],
            CONFIG["forum_activity_id"],
            CONFIG["forum_discussion_id"],
            CONFIG["forum_reply_id"],
        )

    @task(3)
    def browse_course_flow(self):
        course_id, page_id, forum_id, _, _ = self._ids()

        self._send_request("GET", "/", name="Frontpage logged")
        course = self._send_request("GET", f"/course/view.php?id={course_id}", name="View course")
        if not self.sesskey:
            self.sesskey = extract([r"sesskey=([^\"&]+)", r'"sesskey":"([^"]+)"'], course.text)

        self._send_request("GET", f"/mod/page/view.php?id={page_id}", name="View a page activity")
        self._send_request("GET", f"/course/view.php?id={course_id}", name="View course again")
        self._send_request("GET", f"/mod/forum/view.php?id={forum_id}", name="View a forum activity")

    @task(2)
    def forum_read_flow(self):
        course_id, _, forum_id, discussion_id, _ = self._ids()

        self._send_request("GET", f"/course/view.php?id={course_id}", name="View course")
        self._send_request("GET", f"/mod/forum/view.php?id={forum_id}", name="View a forum activity")
        self._send_request("GET", f"/mod/forum/discuss.php?d={discussion_id}", name="View a forum discussion")

    @task(1)
    def forum_reply_flow(self):
        course_id = CONFIG["course_id"]
        discussion_id = CONFIG["forum_discussion_id"]
        reply_id = CONFIG["forum_reply_id"]

        self._send_request("GET", f"/mod/forum/discuss.php?d={discussion_id}", name="View a forum discussion")
        form = self._send_request("GET", f"/mod/forum/post.php?reply={reply_id}", name="Fill a form to reply a forum discussion")
        userid = extract([r'name="userid"\s+type="hidden"\s+value="(\d+)"', r'value="(\d+)"\s+name="userid"'], form.text, "0")
        sesskey = extract([r'name="sesskey"\s+type="hidden"\s+value="([^"]+)"', r'value="([^"]+)"\s+name="sesskey"', r"sesskey=([^\"&]+)"], form.text, self.sesskey)
        attachments = extract([r'value="(\d+)"\s+name="attachments"\s+type="hidden"', r'name="attachments"\s+type="hidden"\s+value="(\d+)"'], form.text, "0")
        itemid = extract([r'type="hidden"\s+name="message\[itemid\]"\s+value="(\d+)"', r'name="message\[itemid\]"\s+type="hidden"\s+value="(\d+)"'], form.text, "0")

        if sesskey:
            suffix = random.randint(100000, 999999)
            self._send_request(
                "POST",
                "/mod/forum/post.php",
                name="Send the forum discussion reply",
                data={
                    "course": str(course_id),
                    "forum": "0",
                    "discussion": str(discussion_id),
                    "userid": userid,
                    "groupid": "0",
                    "edit": "0",
                    "reply": str(reply_id),
                    "sesskey": sesskey,
                    "_qf__mod_forum_post_form": "1",
                    "subject": f"Re: Locust reply {suffix}",
                    "message[itemid]": itemid,
                    "message[format]": "1",
                    "message[text]": f"Locust Moodle forum reply {suffix}",
                    "parent": str(reply_id),
                    "subscribe": "1",
                    "attachments": attachments,
                    "timestart": "0",
                    "timeend": "0",
                    "submitbutton": "Post to forum",
                },
                allow_redirects=True,
            )

    @task(1)
    def participants_flow(self):
        course_id = CONFIG["course_id"]

        self._send_request("GET", f"/course/view.php?id={course_id}", name="View course once more")
        self._send_request("GET", f"/user/index.php?id={course_id}", name="View course participants")

    def on_stop(self):
        if self.sesskey:
            self._send_request("GET", f"/login/logout.php?sesskey={self.sesskey}", name="Logout")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== Moodle Locust test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Course ID: {CONFIG['course_id']}")
    print(f"User CSV: {USERS_CSV}")
    print(f"Available users: {len(USERS_LIST)}")
