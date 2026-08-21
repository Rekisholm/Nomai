import psycopg2
import psycopg2.extras
import json
import time
import logging
import datetime
import os
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
    for change in changes:
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

        if kind == "insert":
            cols, vals = change.get("columnnames", []), change.get("columnvalues", [])
            if pk_field in cols:
                pk_value = vals[cols.index(pk_field)]
        elif kind == "update":
            cols, vals = change.get("columnnames", []), change.get("columnvalues", [])
            # Primary key after the update.
            new_pk = None
            if pk_field in cols:
                new_pk = vals[cols.index(pk_field)]

            # Primary key before the update.
            old = change.get("oldkeys", {})
            old_names = old.get("keynames", [])
            old_values = old.get("keyvalues", [])

            old_pk = None
            if pk_field in old_names:
                old_pk = old_values[old_names.index(pk_field)]

            # pks always records the post-update primary key to keep element types consistent.
            pk_value = new_pk if new_pk is not None else old_pk

            # Record primary-key changes separately.
            if old_pk is not None and new_pk is not None and old_pk != new_pk:
                pk_change = {
                    "before": old_pk,
                    "after": new_pk
                }

        elif kind == "delete":
            old = change.get("oldkeys", {})
            if pk_field in old.get("keynames", []):
                pk_value = old.get("keyvalues", [None])[0]

        if pk_value is None:
            continue

        entries.append({
            "tbn": table,
            "op": kind,
            "pk": pk_value,
            "pk_change": pk_change  # Present only for updates that change the primary key.
        })

    # --- Step 3: Aggregate primary keys by (table + operation) and write logs ---
    if entries:
        current_time = datetime.datetime.now().astimezone().isoformat()
        
        # 1. Aggregation: Key = (table, operation), Value = [pk1, pk2, ...].
        aggregated_data = {}
        
        for e in entries:
            key = (e["tbn"], e["op"])
            if key not in aggregated_data:
                aggregated_data[key] = {
                    "pks": [],
                    "pk_changes": []
                }
            aggregated_data[key]["pks"].append(e["pk"])
            if e["pk_change"] is not None:
                aggregated_data[key]["pk_changes"].append(e["pk_change"])
        
        # 2. Write each aggregated result.
        for (tbn, op), result in aggregated_data.items():
            log_entry = {
                "time": current_time,
                "event": op,
                "rid": request_id,
                "tbn": tbn,
                "pks": result["pks"],  # All primary keys for this table/operation batch.
            }
            if result["pk_changes"]:
                log_entry["pk_changes"] = result["pk_changes"]  # Add only when primary-key changes exist.
            
            line = json.dumps(log_entry, ensure_ascii=False)
            print(line)
            logging.info(line)

    # --- Step 4: Tell PostgreSQL this LSN has been processed ---
    msg.cursor.send_feedback(flush_lsn=msg.data_start)


def start_cdc():
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
