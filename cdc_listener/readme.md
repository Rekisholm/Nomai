# Cell-Level CDC Listener

`listener_cell.py` consumes PostgreSQL `wal2json` logical replication events and
writes cell-level database write logs for `analysis_cell`.

It records database writes only. Database reads are collected by the SQL hooks
inside each scenario.

## Configuration

Choose the CDC config with `CVE_NAME`:

```bash
export CVE_NAME=airflow
```

This loads:

```text
cdc_listener/config_airflow.json
```

Config shape:

```json
{
  "connection": {
    "host": "localhost",
    "port": 5432,
    "database": "airflow",
    "user": "airflow",
    "password": "airflow"
  },
  "table_info": {
    "public.dag": "dag_id",
    "public.dag_run": "id"
  }
}
```

`table_info` maps each watched table to its row identifier column. Changes to
tables not listed in `table_info` are ignored.

## Recommended Run

Use the unified scene entrypoint and enable cell-level CDC:

```bash
conda run -n py312 --no-capture-output \
  ./run_scene.sh start <experiment> <scene> --cell-cdc
```

The scene scripts set:

```text
CVE_NAME=<scene CDC config name>
CDC_LOG_FILE=runs/<experiment>/<scene>/request_db_write.log
```

Listener stdout/stderr is written to:

```text
runs/<experiment>/<scene>/cdc_listener.out
```

## Direct Run

You can also start the listener manually:

```bash
CVE_NAME=airflow \
CDC_LOG_FILE=/tmp/request_db_write.log \
conda run -n py312 --no-capture-output \
  python -u cdc_listener/listener_cell.py
```

If `CDC_LOG_FILE` is not set, the listener writes `request_db_write.log` in the
current directory.

## Output

The output is JSONL: one database row change per line.

INSERT example:

```json
{"time":"2026-07-29 12:32:03.144956+00","event":"insert","rid":"40.3","tbn":"dag_run","pk":1,"xid":124,"seq":0,"cells":[{"col":"id","new":1},{"col":"state","new":"running"}]}
```

UPDATE example:

```json
{"time":"2026-07-29 12:32:03.112366+00","event":"update","rid":"40.2","tbn":"dag","pk":"example_dag","xid":125,"seq":1,"cells":[{"col":"is_paused","old":true,"new":false}]}
```

DELETE example:

```json
{"time":"2026-07-29 12:32:03.112366+00","event":"delete","rid":"40.2","tbn":"dag_run","pk":1,"xid":126,"seq":2,"cells":[]}
```

`cells` must contain only columns changed by the current write. This is required
for cell-level provenance.

## Checks

- PostgreSQL must use `wal_level=logical`.
- `wal2json` must be installed.
- The database user must be allowed to use logical replication and alter replica
  identity for watched tables.
- Transactions must contain the request logical message used to recover `rid`.
- If the log stays empty, check `runs/<experiment>/<scene>/cdc_listener.out`,
  `CVE_NAME`, the matching config file, and whether requests are writing the
  logical message.
