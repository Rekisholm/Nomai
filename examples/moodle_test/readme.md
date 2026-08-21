# Moodle CVE-2024-43425

Moodle CVE-2024-43425 is a command injection vulnerability that enables remote code execution.

## Login

Username: admin
Password: Aa!123456

## Basic Steps

Log in as a role that can create questions, such as an administrator or teacher account.

Create a quiz and add a calculated question that contains the malicious payload.

Bind a dataset to the question to trigger remote code execution.

See `attack.py` in this directory for details.

Reference:
https://www.rapid7.com/db/modules/exploit/linux/http/moodle_rce/

## JMeter Test

https://docs.moodle.org/dev/Load_testing_Moodle_with_JMeter

Log in as administrator and enable debug mode.

Generate test courses and test users.

Generate the JMeter test script and refresh user passwords.

At this point the admin password is also changed to `moodle`.