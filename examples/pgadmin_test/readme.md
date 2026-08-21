# pgAdmin <= 7.6 Authenticated Remote Command Execution (CVE-2023-5002)

pgAdmin is a well-known PostgreSQL database management platform.

pgAdmin includes an HTTP API that lets users select and validate additional PostgreSQL tools such as pg_dump and pg_restore. In CVE-2022-4223, this API could be used for arbitrary command execution. The official fix was incomplete in versions 7.6 and earlier, so authenticated users can still execute arbitrary commands.

## Startup

```bash
docker compose up -d

# Credentials
username: vulhub@example.com
password: vulhub
```

## Reference

https://github.com/vulhub/vulhub/tree/master/pgadmin/CVE-2023-5002
