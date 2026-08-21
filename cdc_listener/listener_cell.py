import psycopg2
import psycopg2.extras
import json
import time
import logging
import datetime
import os
from psycopg2 import sql
from loader import load_config

# Load configuration from the loader module.
CONFIG = load_config()
CONN = CONFIG.get("connection", {})
PG_CONN = f"host={CONN['host']} port={CONN['port']} user={CONN['user']} password={CONN['password']} dbname={CONN['database']}"
TARGETS = CONFIG.get("table_info", {})

SLOT_NAME = "slot_userlog"
PLUGIN = "wal2json"
LOG_FILE = os.environ.get("CDC_LOG_FILE", "request_db_write.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(message)s", 
)

def ensure_slot(conn):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s;", (SLOT_NAME,))
    if cur.fetchone() is None:
        print(f"Creating replication slot '{SLOT_NAME}'...")
        cur.execute(
            "SELECT * FROM pg_create_logical_replication_slot(%s, %s);",
            (SLOT_NAME, PLUGIN),
        )
        print(f"Slot created: {cur.fetchone()}")
        conn.commit()
    cur.close()


def ensure_replica_identity_full():
    """Expose complete old rows so UPDATE cells can be computed by diff."""
    with psycopg2.connect(PG_CONN) as conn:
        with conn.cursor() as cur:
            configured = 0
            for full_tbn in TARGETS:
                schema, table = full_tbn.split(".", 1)
                cur.execute("SELECT to_regclass(%s)", (full_tbn,))
                if cur.fetchone()[0] is None:
                    continue
                cur.execute(
                    sql.SQL("ALTER TABLE {}.{} REPLICA IDENTITY FULL").format(
                        sql.Identifier(schema),
                        sql.Identifier(table),
                    )
                )
                configured += 1
    print(f"Replica identity FULL configured for {configured} target tables.")


def values_by_column(names, values):
    return dict(zip(names, values))


def consume(msg):
    try:
        data = json.loads(msg.payload)
    except Exception as e:
        print(f"[WARN] JSON parse error: {e}")
        msg.cursor.send_feedback(flush_lsn=msg.data_start)
        return
    # print(data)
    changes = data.get("change", [])
    if not changes:
        msg.cursor.send_feedback(flush_lsn=msg.data_start)
        return

    # wal2json emits one xid for the whole committed transaction. Keep it as
    # the database ordering key; timestamps remain only for legacy replay.
    raw_xid = data.get("xid")
    try:
        xid = int(raw_xid) if raw_xid is not None else None
    except (TypeError, ValueError):
        print(f"[WARN] Invalid transaction xid: {raw_xid!r}")
        xid = None

    # --- Step 1: Extract request_id for this transaction ---
    request_id = None
    for ch in changes:
        if ch.get("kind") == "message" and ch.get("prefix") == "request":
            try:
                content = json.loads(ch.get("content", "{}"))
                request_id = content.get("request_id")
                break
            except Exception:
                continue

    # Drop transactions that have no request_id.
    if not request_id:
        msg.cursor.send_feedback(flush_lsn=msg.data_start)
        return

    # --- Step 2: Collect changes to target tables in this transaction ---
    entries = []
    write_seq = 0
    for change in changes:
        # print(change)
        kind = change.get("kind")
        if kind in ("message", "truncate"):
            continue

        schema = change.get("schema")
        table = change.get("table")
        full_tbn = f"{schema}.{table}"
        if full_tbn not in TARGETS:
            continue

        pk_field = TARGETS[full_tbn]
        pk_value = None
        pk_change = None

        cells = []
        if kind == "insert":
            cols, vals = change.get("columnnames", []), change.get("columnvalues", [])
            if pk_field in cols:
                pk_value = vals[cols.index(pk_field)]
            # Inserts record new values only.
            cells = [{"col": col, "new": val} for col, val in zip(cols, vals)]
        elif kind == "update":
            cols, vals = change.get("columnnames", []), change.get("columnvalues", [])
            old = change.get("oldkeys", {})
            old_names = old.get("keynames", [])
            old_values = old.get("keyvalues", [])
            old_row = values_by_column(old_names, old_values)
            new_row = values_by_column(cols, vals)
            # wal2json omits NULL-valued columns from oldkeys. They are safe to
            # reconstruct only when the new tuple is also NULL. When the old row
            # is incomplete despite REPLICA IDENTITY FULL (e.g. login-tracking
            # columns), over-approximate: treat missing columns as changed with
            # old=None rather than killing the whole CDC stream.
            missing_old_columns = [
                col
                for col in cols
                if col not in old_row and new_row[col] is not None
            ]
            if missing_old_columns:
                print(
                    f"[WARN] UPDATE old row incomplete for {full_tbn}; "
                    f"over-approximating missing columns: {missing_old_columns}"
                )
            if pk_field in cols:
                pk_value = vals[cols.index(pk_field)]
            elif pk_field in old_row:
                pk_value = old_row[pk_field]
            old_pk = old_row.get(pk_field)
            if old_pk is not None and pk_value is not None and old_pk != pk_value:
                pk_change = {"before": old_pk, "after": pk_value}
            if pk_change is not None:
                cells = [
                    {"col": col, "new": val}
                    for col, val in new_row.items()
                ]
            else:
                cells = [
                    {"col": col, "old": old_row.get(col), "new": val}
                    for col, val in new_row.items()
                    if old_row.get(col) != val
                ]
        elif kind == "delete":
            old = change.get("oldkeys", {})
            old_row = values_by_column(
                old.get("keynames", []),
                old.get("keyvalues", []),
            )
            if pk_field in old_row:
                pk_value = old_row[pk_field]
            # Deletes do not record cell changes.
            cells = []

        if pk_value is None:
            continue

        entries.append({
            "tbn": table,
            "op": kind,
            "pk": pk_value,
            "cells": cells,
            "pk_change": pk_change,
            "seq": write_seq,
        })
        write_seq += 1

    # --- Step 3: Write one cell-level log entry per row ---
    if entries:
        current_time = data.get("timestamp")
        if not current_time:
            current_time = datetime.datetime.now().astimezone().isoformat()
        for e in entries:
            if e["pk_change"] is not None:
                old_entry = {
                    "time": current_time,
                    "event": "delete",
                    "rid": request_id,
                    "tbn": e["tbn"],
                    "pk": e["pk_change"]["before"],
                    "cells": [],
                    "seq": e["seq"],
                }
                new_entry = {
                    "time": current_time,
                    "event": "insert",
                    "rid": request_id,
                    "tbn": e["tbn"],
                    "pk": e["pk_change"]["after"],
                    "cells": e["cells"],
                    "seq": e["seq"],
                }
                for log_entry in (old_entry, new_entry):
                    if xid is not None:
                        log_entry["xid"] = xid
                    line = json.dumps(log_entry, ensure_ascii=False)
                    print(line)
                    logging.info(line)
                continue
            log_entry = {
                "time": current_time,
                "event": e["op"],
                "rid": request_id,
                "tbn": e["tbn"],
                "pk": e["pk"],
                "cells": e["cells"],
                "seq": e["seq"],
            }
            if xid is not None:
                log_entry["xid"] = xid
            line = json.dumps(log_entry, ensure_ascii=False)
            print(line)
            logging.info(line)

    # --- Step 4: Tell PostgreSQL this LSN has been processed ---
    msg.cursor.send_feedback(flush_lsn=msg.data_start)


def start_cdc():
    ensure_replica_identity_full()
    while True:
        try:
            print("Connecting to replication stream...")
            conn = psycopg2.connect(
                PG_CONN,
                connection_factory=psycopg2.extras.LogicalReplicationConnection,
            )
            ensure_slot(conn)
            cur = conn.cursor()

            add_tables = ",".join(TARGETS.keys())
            options = {
                "add-tables": add_tables,
                "include-pk": "1",
                "include-xids": "1",
                "include-timestamp": "1",
                "pretty-print": "0",
            }

            print(f"Listening for changes on {list(TARGETS.keys())}...")
            cur.start_replication(slot_name=SLOT_NAME, decode=True, options=options)
            cur.consume_stream(consume)

        except KeyboardInterrupt:
            print("\n[INFO] Stopped by user.")
            break
        except Exception as e:
            print(f"[WARN] Connection error: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    start_cdc()
