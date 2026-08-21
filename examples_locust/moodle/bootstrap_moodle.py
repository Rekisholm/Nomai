#!/usr/bin/env python3
import csv
import json
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "moodle_locust_config.json"
USERS_PATH = ROOT / "locust_users.csv"

MOODLE_CONTAINER = os.getenv("MOODLE_CONTAINER", "moodle_test-moodle-1")
POSTGRES_CONTAINER = os.getenv("MOODLE_POSTGRES_CONTAINER", "moodle_test-postgresql-1")
BASE_URL = os.getenv("MOODLE_BASE_URL", "http://10.0.0.252:8081")
COURSE_SHORTNAME = os.getenv("MOODLE_COURSE_SHORTNAME", f"locust_{int(time.time())}")
COURSE_FULLNAME = os.getenv("MOODLE_COURSE_FULLNAME", "Locust Moodle Test Course")
COURSE_SIZE = os.getenv("MOODLE_COURSE_SIZE", "S")
USER_PASSWORD = os.getenv("MOODLE_GENERATED_USER_PASSWORD", "moodle")
ADMIN_USERNAME = os.getenv("MOODLE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("MOODLE_ADMIN_PASSWORD", "Aa!123456")


def run(cmd, capture=True):
    print("+ " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    ).stdout if capture else ""


def docker_exec(container, command):
    return run(["docker", "exec", container, "sh", "-lc", command])


def psql_value(sql):
    escaped = sql.replace('"', '\\"')
    out = docker_exec(
        POSTGRES_CONTAINER,
        f'psql -U moodle -d moodle -Atc "{escaped}"',
    )
    return out.strip()


def psql_rows(sql):
    value = psql_value(sql)
    if not value:
        return []
    return [line.split("|") for line in value.splitlines()]


def create_course():
    cmd = (
        "cd /var/www/html && "
        "php admin/tool/generator/cli/maketestcourse.php "
        f"--shortname={COURSE_SHORTNAME} "
        f"--fullname='{COURSE_FULLNAME}' "
        f"--size={COURSE_SIZE} "
        "--bypasscheck "
        "--additionalmodules=page,forum"
    )
    docker_exec(MOODLE_CONTAINER, cmd)

    # This updates generated course users to $CFG->tool_generator_users_password.
    plan_cmd = (
        "cd /var/www/html && "
        "php admin/tool/generator/cli/maketestplan.php "
        f"--shortname={COURSE_SHORTNAME} "
        f"--size={COURSE_SIZE} "
        "--bypasscheck "
        "--updateuserspassword"
    )
    docker_exec(MOODLE_CONTAINER, plan_cmd)


def fetch_course_data():
    shortname = COURSE_SHORTNAME.replace("'", "''")
    course_id = psql_value(f"select id from mdl_course where shortname='{shortname}' order by id desc limit 1;")
    if not course_id:
        raise RuntimeError(f"course not found: {COURSE_SHORTNAME}")

    module_rows = psql_rows(
        "select m.name, cm.id "
        "from mdl_course_modules cm join mdl_modules m on m.id = cm.module "
        f"where cm.course = {course_id} and m.name in ('page','forum') "
        "order by cm.id;"
    )
    modules = {}
    for name, cmid in module_rows:
        modules.setdefault(name, cmid)

    discussion = psql_rows(
        "select d.id, d.firstpost "
        "from mdl_forum_discussions d "
        f"where d.course = {course_id} "
        "order by d.id limit 1;"
    )
    discussion_id, first_post_id = discussion[0] if discussion else ("", "")

    users = psql_rows(
        "select distinct u.username "
        "from mdl_user u "
        "join mdl_user_enrolments ue on ue.userid = u.id "
        "join mdl_enrol e on e.id = ue.enrolid "
        f"where e.courseid = {course_id} and u.deleted = 0 and u.suspended = 0 "
        "and u.username like 'tool_generator_%' "
        "order by u.username;"
    )

    return {
        "base_url": BASE_URL,
        "course_shortname": COURSE_SHORTNAME,
        "course_id": int(course_id),
        "page_activity_id": int(modules["page"]) if modules.get("page") else None,
        "forum_activity_id": int(modules["forum"]) if modules.get("forum") else None,
        "forum_discussion_id": int(discussion_id) if discussion_id else None,
        "forum_reply_id": int(first_post_id) if first_post_id else None,
        "users": [row[0] for row in users],
    }


def reset_user_passwords(usernames):
    for username in usernames:
        safe_username = username.replace("'", "'\"'\"'")
        safe_password = USER_PASSWORD.replace("'", "'\"'\"'")
        docker_exec(
            MOODLE_CONTAINER,
            "cd /var/www/html && "
            "php admin/cli/reset_password.php "
            f"--username='{safe_username}' "
            f"--password='{safe_password}' "
            "--ignore-password-policy",
        )


def write_outputs(config):
    if not config["users"]:
        raise RuntimeError("no generated enrolled users found")

    with USERS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for username in config["users"]:
            writer.writerow([username, USER_PASSWORD])

    locust_config = {key: value for key, value in config.items() if key != "users"}
    locust_config["users_csv"] = USERS_PATH.name
    locust_config["admin_username"] = ADMIN_USERNAME
    locust_config["admin_password"] = ADMIN_PASSWORD
    CONFIG_PATH.write_text(json.dumps(locust_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {CONFIG_PATH}")
    print(f"wrote {USERS_PATH} ({len(config['users'])} users)")


def main():
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,10.0.0.252")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,10.0.0.252")
    create_course()
    config = fetch_course_data()
    missing = [key for key in ("page_activity_id", "forum_activity_id", "forum_discussion_id", "forum_reply_id") if not config.get(key)]
    if missing:
        raise RuntimeError(f"generated course is missing required data: {', '.join(missing)}")
    reset_user_passwords(config["users"])
    write_outputs(config)


if __name__ == "__main__":
    main()
