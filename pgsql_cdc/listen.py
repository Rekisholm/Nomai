import psycopg2
import psycopg2.extras
import json
import time

PG_CONN = "host=localhost port=5434 user=postgres password=postgres dbname=pgcdc"

SLOT_NAME = "slot_userlog"
PLUGIN = "wal2json"
TARGET_TABLE = "public.user_log"
PRIMARY_KEY = "id"  # Assumes the primary key is id.


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
    """
    PostgreSQL pushes one transaction change at a time, including the full
    change batch. Parse INSERT/UPDATE/DELETE events for the target table,
    collect primary key IDs by operation type, and print one summary.
    """
    try:
        data = json.loads(msg.payload)
        print(f"Data: {data}")
    except Exception:
        print("Failed to parse JSON:", msg.payload)
        msg.cursor.send_feedback(flush_lsn=msg.data_start)
        return

    changes = data.get("change", [])
    inserts, updates, deletes = set(), set(), set()

    for change in changes:
        
        table = f"{change.get('schema')}.{change.get('table')}"
        if table != TARGET_TABLE:
            continue

        kind = change.get("kind")

        # Extract the primary key from the new or old values.
        pk_value = None

        if kind == "insert":
            cols = change.get("columnnames", [])
            vals = change.get("columnvalues", [])
            if PRIMARY_KEY in cols:
                pk_value = vals[cols.index(PRIMARY_KEY)]

        elif kind == "update":
            # Prefer the new values, then fall back to old keys.
            cols = change.get("columnnames", [])
            vals = change.get("columnvalues", [])
            if PRIMARY_KEY in cols:
                pk_value = vals[cols.index(PRIMARY_KEY)]
            else:
                old = change.get("oldkeys", {})
                if PRIMARY_KEY in old.get("keynames", []):
                    pk_value = old.get("keyvalues", [None])[0]

        elif kind == "delete":
            old = change.get("oldkeys", {})
            if PRIMARY_KEY in old.get("keynames", []):
                pk_value = old.get("keyvalues", [None])[0]

        if pk_value is not None:
            if kind == "insert":
                inserts.add(pk_value)
            elif kind == "update":
                updates.add(pk_value)
            elif kind == "delete":
                deletes.add(pk_value)

    # Print a summary if this transaction touched the target table.
    if inserts or updates or deletes:
        xid = data.get("xid")
        print(f"\nTransaction {xid} summary on {TARGET_TABLE}:")
        print(f"  INSERT ids: {sorted(inserts) or '-'}")
        print(f"  UPDATE ids: {sorted(updates) or '-'}")
        print(f"  DELETE ids: {sorted(deletes) or '-'}")

    # Tell PostgreSQL that this LSN has been processed.
    msg.cursor.send_feedback(flush_lsn=msg.data_start)


def start_cdc():
    """Start the logical replication stream and keep listening for commits."""
    while True:
        try:
            print("Connecting to replication stream...")
            conn = psycopg2.connect(
                PG_CONN,
                connection_factory=psycopg2.extras.LogicalReplicationConnection,
            )
            ensure_slot(conn)
            cur = conn.cursor()

            options = {
                "add-tables": TARGET_TABLE,
                "include-pk": "1",
                "include-xids": "1",
                "pretty-print": "0",
            }

            print(f"Listening for changes on {TARGET_TABLE}...")
            cur.start_replication(slot_name=SLOT_NAME, decode=True, options=options)
            cur.consume_stream(consume)

        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"Connection error: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    start_cdc()
