# GitLab 14.1.0 (CVE-2022-2230)

Stored XSS vulnerability from **VoAPI**.

## Start the Scene

```bash
# Start the scene
docker compose up -d

# Get the root administrator password
docker exec -it gitlab-ce14 cat /etc/gitlab/initial_root_password | grep Password:
```

## Login Accounts

- Administrator account: `root`
- Administrator password: `8ceuzkEbv9aZx8iCiImeH6GbJ5QHZpQhwa530clm7Ik==`

- Normal user account: `user1`
- Normal user password: `12345678`

## Reference

CVE-2022-2230

https://hackerone.com/reports/1588732

## Steps

Run `python ./attack_xss.py`.
Log in as user `user1`.
Open Settings/Repository.
Expand Protected branches.
Expand the third search box, Allowed to push.
Trigger this request:

```
GET http://10.0.0.252:8080/-/autocomplete/deploy_keys_with_owners.json?search=&per_page=20&active=true&project_id=594&push_code=true

Response Code: 304
```

An alert box should pop up.

## Project Fork 500 Vulnerability from Miner

See `attack_fork.py` in this directory.
