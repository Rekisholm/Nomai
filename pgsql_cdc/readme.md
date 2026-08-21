# PgSQL CDC

The PgSQL image includes the compiled `wal2json.so` plugin.
After startup, CDC (Change Data Capture) is available for capturing DML operations (INSERT, UPDATE, DELETE) on database tables.

Docker build command:
```
docker build -t pg-request-logger:13.12 .
```

Usage:
```
-- Create a test table.
CREATE TABLE user_log (
  id SERIAL PRIMARY KEY,
  username TEXT,
  action TEXT,
  ts TIMESTAMP DEFAULT now()
);

BEGIN;
INSERT INTO user_log(username, action) VALUES ('alice','login');
UPDATE user_log SET action='logout' WHERE username='alice';
DELETE FROM user_log WHERE username='alice';
COMMIT;

```

Start the Python listener to consume CDC logs with transaction support:
python listen.py

## Active Statement Snapshot Bounds

The image also installs the `request_id_logger` PostgreSQL extension and automatically creates it for the current application database and `template1` when a new data directory is initialized. The SQL rewriter reads the active snapshot currently used by the executor through these two functions:

```sql
SELECT nomai_snapshot_bounds_xmin(), nomai_snapshot_bounds_xmax();
```

For existing data volumes, run this once in each application database:

```sql
CREATE EXTENSION IF NOT EXISTS request_id_logger;
```

Use the following plan check to confirm that the rewriter's two uncorrelated scalar subqueries are treated as one-time `InitPlan` nodes:

```sql
EXPLAIN (COSTS OFF)
SELECT
  (SELECT nomai_snapshot_bounds_xmin()) AS __nomai_xmin,
  (SELECT nomai_snapshot_bounds_xmax()) AS __nomai_xmax;
```
