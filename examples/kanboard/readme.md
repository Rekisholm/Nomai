# Kanboard XSS (CVE-2025-46825)

Stored XSS in the project name.

## Startup

```bash
docker compose up -d

# Credentials
admin/admin

# Code path
cd /var/www/app/app/Template/project_view
```

## Attack Steps

1. After startup, visit `http://localhost:8080` and log in with the default credentials.
2. Create a normal project. Click New Project, set the name field to `project1`, fill other fields arbitrarily, and save.
3. Create a malicious project. Click New Project, set the name field to `<meta http-equiv="refresh" content="2;url=http://example.com/" />`, fill other fields arbitrarily, and save.
4. Visit `http://localhost:8080/project/1/import/tasks` and wait 2 seconds for the automatic redirect to `http://example.com`.

## Reference

https://github.com/kanboard/kanboard/security/advisories/GHSA-5wj3-c9v4-pj9v

https://nvd.nist.gov/vuln/detail/cve-2025-46825
