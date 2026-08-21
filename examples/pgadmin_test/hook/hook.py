import re
import flask
import ctypes
import threading
import time
import os
import json
import datetime
import weakref
import uuid
from typing import List, Dict, Any

# --------- Logging ---------
os.makedirs("/tmp/logs", exist_ok=True)
libc = ctypes.CDLL(None)
log_file = open("/tmp/logs/request_db_read.log", "a", buffering=1)
log_file_debug = open("/tmp/logs/debug.log", "a", buffering=1)
request_context_file = open("/tmp/logs/request_context.log", "a", buffering=1)

SNAPSHOT_COLUMN_COUNT = 2
SNAPSHOT_XMIN_ALIAS = "__nomai_xmin"
SNAPSHOT_XMAX_ALIAS = "__nomai_xmax"
REQUEST_ID_SET_RE = re.compile(
    r"set_config\s*\(\s*'request_id'\s*,\s*'((?:''|[^'])*)'",
    re.IGNORECASE,
)

def _emit(line: str):
    try:
        log_file.write(line + "\n")
    except Exception:
        pass

def log_debug(msg):
    try:
        log_file_debug.write(msg + "\n")
    except Exception:
        pass

def _emit_request_context(record: dict):
    if request_context_file is None:
        return
    try:
        request_context_file.write(json.dumps(record) + "\n")
    except Exception:
        pass

# The pgAdmin attack reuses one X-Request-ID for connect/initialize/start/poll in the same logical step,
# This can associate one RID with multiple api_id values. Deduplicate by RID and keep the first context; user_id is consistent and does not affect W(c).
_context_seen_rids = set()
_context_seen_lock = threading.Lock()

def _emit_request_context_dedup(record: dict):
    rid = record.get("rid")
    if rid is None:
        return
    try:
        with _context_seen_lock:
            if rid in _context_seen_rids:
                return
            _context_seen_rids.add(rid)
        _emit_request_context(record)
    except Exception:
        pass

import hashlib

def _request_user_identity(rid: str):
    """Prefer the Flask-Login user ID; otherwise use a session cookie digest; finally fall back to request:<rid>."""
    try:
        from flask_login import current_user
        if current_user.is_authenticated:
            user_id = current_user.get_id()
            if user_id is not None:
                return f"user:{user_id}", "flask_login"
    except Exception as e:
        log_debug(f"Failed to read flask_login.current_user: {e}")
    try:
        cookie_name = flask.current_app.config.get("SESSION_COOKIE_NAME", "pga_session")
        cookie = flask.request.cookies.get(cookie_name)
        if cookie:
            digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:24]
            return f"session:{digest}", "session_cookie_sha256"
    except Exception as e:
        log_debug(f"Failed to read session cookie: {e}")
    return f"request:{rid}", "request_id"

def _request_api_id():
    """api_id = method + Flask url_rule.rule (route template), falling back to path."""
    try:
        method = flask.request.method.upper()
        route = (
            flask.request.url_rule.rule
            if flask.request.url_rule is not None
            else flask.request.path
        )
        return f"{method} {route}", route
    except Exception as e:
        log_debug(f"Failed to get api_id: {e}")
    return "GET /", "/"

# ============ State storage (works around __slots__) ============
# Store additional cursor state in a weak-reference dictionary
# Key: cursor object
# Value: dict, e.g. {"meta": [...], "recursion": False}
_cursor_state = weakref.WeakKeyDictionary()

def get_cursor_state(cursor, key, default=None):
    """Safely get cursor state"""
    state = _cursor_state.get(cursor, {})
    return state.get(key, default)

def set_cursor_state(cursor, key, value):
    """Safely set cursor state"""
    if cursor not in _cursor_state:
        _cursor_state[cursor] = {}
    _cursor_state[cursor][key] = value

# ============ Try to load the Rust extension library ============
try:
    from hook_lib import call_rewrite_sql
    RUST_LIB_AVAILABLE = True
except ImportError:
    RUST_LIB_AVAILABLE = False
    log_debug("[hook] Warning: hook_lib not found. SQL rewrite disabled.")
    def call_rewrite_sql(sql): return None, None

# --------- Global request id  ---------
_local = threading.local()

def next_request_id() -> str:
    pid = os.getpid()
    tid = threading.get_ident()
    rid= str(uuid.uuid4())
    return f"{pid}-{tid}-{rid}"

def current_request_id(default=None):
    try:
        return getattr(flask.g, "request_id", default)
    except RuntimeError:
        return default

def reset_read_sequence():
    _local.read_sequence = 0

def next_read_sequence() -> int:
    sequence = getattr(_local, "read_sequence", 0)
    _local.read_sequence = sequence + 1
    return sequence

def request_id_from_query(query):
    if isinstance(query, bytes):
        text = query.decode("utf-8", errors="ignore")
    elif isinstance(query, str):
        text = query
    else:
        return None
    match = REQUEST_ID_SET_RE.search(text)
    if not match:
        return None
    return match.group(1).replace("''", "'")

# --------- Core helper functions shared by pgAdmin and generic hooks ---------
_conn_last_rid = weakref.WeakKeyDictionary()

def _inject_request_id_db(cursor, rid, execute_func=None):
    """Inject request_id; set it only once per connection and RID."""
    try:
        conn = cursor.connection
        if not conn:
            return
        if _conn_last_rid.get(conn) == rid:
            return

        safe_rid = str(rid).replace("'", "''")
        # Mark first to avoid recursive injection when conn.execute() creates an internal cursor.
        _conn_last_rid[conn] = rid
        set_sql = f"SET request_id = '{safe_rid}'"
        if execute_func is not None:
            execute_func(cursor, set_sql)
        else:
            conn.execute(set_sql)
    except Exception as e:
        log_debug(f"inject request_id error: {e}")
        pass

def _snapshot_alias(meta_list, key, fallback):
    if meta_list and meta_list[0].get(key):
        return meta_list[0][key]
    return fallback

def _find_mapping_key(row, alias, fallback_key=None):
    if alias:
        try:
            if alias in row:
                return alias
        except Exception:
            pass

        try:
            keys = list(row.keys())
        except Exception:
            keys = []
        for key in reversed(keys):
            if isinstance(key, str) and key.startswith(alias):
                return key

    if fallback_key is not None:
        try:
            if fallback_key in row:
                return fallback_key
        except Exception:
            pass
    return None

def _pop_mapping_value(row, alias, fallback_key=None):
    key = _find_mapping_key(row, alias, fallback_key)
    if key is None:
        return None

    try:
        value = row.get(key)
    except Exception:
        try:
            value = row[key]
        except Exception:
            return None

    try:
        if isinstance(row, dict):
            row.pop(key, None)
        else:
            del row[key]
    except Exception:
        pass
    return value

def _strip_mapping_row(row, meta_list):
    keys = list(row.keys())
    injected_count = len(meta_list) + SNAPSHOT_COLUMN_COUNT
    if len(keys) < injected_count:
        return None, []

    pk_fallback_keys = keys[-injected_count:-SNAPSHOT_COLUMN_COUNT]
    xmin_fallback_key = keys[-2]
    xmax_fallback_key = keys[-1]
    xmin_alias = _snapshot_alias(meta_list, "snapshot_xmin_alias", SNAPSHOT_XMIN_ALIAS)
    xmax_alias = _snapshot_alias(meta_list, "snapshot_xmax_alias", SNAPSHOT_XMAX_ALIAS)

    xmax = _pop_mapping_value(row, xmax_alias, xmax_fallback_key)
    xmin = _pop_mapping_value(row, xmin_alias, xmin_fallback_key)

    pk_values = []
    for idx, meta in enumerate(meta_list):
        alias = meta.get("alias_used")
        fallback_key = pk_fallback_keys[idx] if idx < len(pk_fallback_keys) else None
        pk_values.append(_pop_mapping_value(row, alias, fallback_key))

    if xmin is None or xmax is None:
        return None, pk_values
    return (int(xmin), int(xmax)), pk_values

def _strip_sequence_row(row, meta_list):
    injected_count = len(meta_list) + SNAPSHOT_COLUMN_COUNT
    if len(row) < injected_count:
        return row, None, []

    pk_start = len(row) - injected_count
    pk_values = [row[pk_start + idx] for idx in range(len(meta_list))]
    xmin = row[-2]
    xmax = row[-1]
    cleaned = row[:-injected_count]

    if xmin is None or xmax is None:
        return cleaned, None, pk_values
    return cleaned, (int(xmin), int(xmax)), pk_values

def _register_snapshot_bounds(cursor, bounds):
    existing = get_cursor_state(cursor, "snapshot_bounds", None)
    if existing is None:
        set_cursor_state(cursor, "snapshot_bounds", bounds)
        return True
    return existing == bounds

def _rewrite_query_if_needed(query):
    if not RUST_LIB_AVAILABLE:
        return query, []

    if isinstance(query, bytes):
        sql_bytes = query
        sql_text = query.decode("utf-8", errors="ignore")
    elif isinstance(query, str):
        sql_text = query
        sql_bytes = query.encode("utf-8")
    else:
        return query, []

    stripped = sql_text.strip().upper()
    if not stripped.startswith(("SELECT", "WITH")):
        return query, []

    new_sql, meta_list = call_rewrite_sql(sql_bytes)
    if new_sql and meta_list:
        return new_sql, meta_list
    return query, []

def _strip_description(desc, meta):
    if not desc or not meta:
        return desc
    injected_count = len(meta) + SNAPSHOT_COLUMN_COUNT
    if len(desc) > injected_count:
        return desc[:-injected_count]
    return desc

def _process_result(cursor, rows):
    """
    Generic result handling:
    1. Extract injected-column data from meta and generate JSON logs
    2. Strip injected columns before returning rows to the application layer
    """
    meta_list = get_cursor_state(cursor, "meta", [])
    
    if not rows: return rows
    if not meta_list: return rows

    rid = get_cursor_state(cursor, "request_id", None) or current_request_id("unknown")
    read_sequence = get_cursor_state(cursor, "read_sequence", None)
    
    try:
        first_row = rows[0]
        is_dict_row = hasattr(first_row, 'keys') or isinstance(first_row, dict)
        audit_buffer = {}
        snapshot_bounds = set()

        if is_dict_row:
            for row in rows:
                bounds, pk_values = _strip_mapping_row(row, meta_list)
                if bounds is not None:
                    snapshot_bounds.add(bounds)
                    if not _register_snapshot_bounds(cursor, bounds):
                        raise ValueError(f"inconsistent snapshot bounds: {bounds}")
                for idx, val in enumerate(pk_values):
                    if val is None:
                        continue
                    table = meta_list[idx].get("table", "unknown").split(".")[-1]
                    audit_buffer.setdefault(table, set()).add(str(val))

        else:
            cleaned_rows = []
            for row in rows:
                cleaned, bounds, pk_values = _strip_sequence_row(row, meta_list)
                cleaned_rows.append(cleaned)
                if bounds is not None:
                    snapshot_bounds.add(bounds)
                    if not _register_snapshot_bounds(cursor, bounds):
                        raise ValueError(f"inconsistent snapshot bounds: {bounds}")
                for idx, val in enumerate(pk_values):
                    if val is None:
                        continue
                    table = meta_list[idx].get("table", "unknown").split(".")[-1]
                    audit_buffer.setdefault(table, set()).add(str(val))
            rows = cleaned_rows

        if audit_buffer and rid != "unknown":
            if len(snapshot_bounds) != 1:
                raise ValueError(f"inconsistent snapshot bounds: {snapshot_bounds}")
            xmin, xmax = next(iter(snapshot_bounds))
            log_time = datetime.datetime.now().astimezone().isoformat()
            for table, pks in audit_buffer.items():
                _emit(json.dumps({
                    "time": log_time,
                    "event": "select",
                    "rid": rid,
                    "tbn": table,
                    "pks": list(pks),
                    "seq": read_sequence,
                    "xmin": xmin,
                    "xmax": xmax,
                }))

    except Exception as e:
        log_debug(f"Process result error: {e}")

    return rows


# --------- Part 1: Generic psycopg3 hook (Full Rewrite Support) ---------
# This instrumentation affects pgAdmin internal business logic, such as login and config loading
try:
    import psycopg
except ImportError:
    psycopg = None

if psycopg:
    _orig_execute = psycopg.Cursor.execute
    _orig_fetchall = psycopg.Cursor.fetchall
    _orig_fetchone = psycopg.Cursor.fetchone
    _orig_fetchmany = psycopg.Cursor.fetchmany
    # Get the original property getter
    _orig_desc_getter = psycopg.Cursor.description.__get__

    def traced_execute_generic(self, query, params=None, **kwargs):
        # 1. Recursion check
        if get_cursor_state(self, "recursion", False):
            return _orig_execute(self, query, params, **kwargs)
        
        set_cursor_state(self, "recursion", True)
        set_cursor_state(self, "meta", []) # Reset meta
        set_cursor_state(self, "read_sequence", None)
        set_cursor_state(self, "snapshot_bounds", None)

        try:
            # 2. Inject Request ID
            rid = current_request_id(None) or request_id_from_query(query)
            set_cursor_state(self, "request_id", rid)
            if rid: 
                _inject_request_id_db(self, rid, _orig_execute)
            
            final_query = query
            try:
                final_query, meta_list = _rewrite_query_if_needed(query)
                if meta_list:
                    set_cursor_state(self, "meta", meta_list)
                    set_cursor_state(self, "read_sequence", next_read_sequence())
            except Exception as e:
                log_debug(f"[Generic-Rewrite] Error: {e}")

            return _orig_execute(self, final_query, params, **kwargs)
        finally:
            set_cursor_state(self, "recursion", False)

    def traced_fetchall_generic(self):
        rows = _orig_fetchall(self)
        # Call the unified processing logic with tuple support
        return _process_result(self, rows)

    def traced_fetchone_generic(self):
        row = _orig_fetchone(self)
        if row:
            # Wrap as a list for processing, then unwrap
            processed = _process_result(self, [row])
            return processed[0] if processed else None
        return None

    def traced_fetchmany_generic(self, size=None):
        rows = _orig_fetchmany(self, size)
        return _process_result(self, rows)

    # *** Important *** Patch Description Property
    # Otherwise the ORM fails because returned column count differs from the expected count
    @property
    def traced_description_generic(self):
        desc = _orig_desc_getter(self)
        meta = get_cursor_state(self, "meta", [])
        return _strip_description(desc, meta)

    # Apply Patch
    psycopg.Cursor.execute = traced_execute_generic
    psycopg.Cursor.fetchall = traced_fetchall_generic
    psycopg.Cursor.fetchone = traced_fetchone_generic
    psycopg.Cursor.fetchmany = traced_fetchmany_generic
    # Patch property using setattr on class
    setattr(psycopg.Cursor, "description", traced_description_generic)


# --------- Part 2: pgAdmin-specific Cursor Hook (Full Rewrite Support) ---------
# This instrumentation mainly affects Query Tool SQL entered manually by users

def _try_patch_pgadmin_cursor():
    import sys, importlib.util
    
    while True:
        try:
            if "pgadmin" not in sys.modules:
                time.sleep(1)
                continue

            # Wait until the pgAdmin-specific cursor module is loaded
            spec3 = importlib.util.find_spec("pgadmin.utils.driver.psycopg3.cursor")
            if spec3 is None:
                time.sleep(1)
                continue

            import pgadmin.utils.driver.psycopg3.cursor as pg_cursor
            
            targets = []
            for cls_name in ["DictCursor", "AsyncDictCursor"]:
                if hasattr(pg_cursor, cls_name):
                    targets.append(getattr(pg_cursor, cls_name))

            if not targets:
                time.sleep(1)
                continue

            log_debug("[hook] patching pgadmin DictCursor & AsyncDictCursor ...")

            def _patch_class_methods(cls):
                orig_execute = cls.execute
                orig_fetchall = getattr(cls, "fetchall", None)
                orig_fetchone = getattr(cls, "fetchone", None)
                orig_fetchmany = getattr(cls, "fetchmany", None)
                orig_desc_getter = cls.description.__get__
                orig_ordered_description = getattr(cls, "ordered_description", None)

                def traced_execute_pgadmin(self, query, params=None, **kwargs):
                    # log_debug(f"[PG-Exec]: {str(query)[:50]}...")
                    
                    if get_cursor_state(self, "recursion", False):
                        return orig_execute(self, query, params, **kwargs)
                    
                    set_cursor_state(self, "recursion", True)
                    set_cursor_state(self, "meta", [])
                    set_cursor_state(self, "read_sequence", None)
                    set_cursor_state(self, "snapshot_bounds", None)

                    try:
                        rid = current_request_id(None) or request_id_from_query(query)
                        set_cursor_state(self, "request_id", rid)
                        if rid: _inject_request_id_db(self, rid, orig_execute)

                        final_query = query
                        try:
                            final_query, meta_list = _rewrite_query_if_needed(query)
                            if meta_list:
                                final_query = final_query
                                set_cursor_state(self, "meta", meta_list)
                                set_cursor_state(self, "read_sequence", next_read_sequence())
                        except Exception as e:
                            log_debug(f"Rewrite error: {e}")

                        return orig_execute(self, final_query, params, **kwargs)
                    finally:
                        set_cursor_state(self, "recursion", False)

                def traced_fetchall_pgadmin(self, *args, **kwargs):
                    rows = orig_fetchall(self, *args, **kwargs) if orig_fetchall else []
                    return _process_result(self, rows)

                def traced_fetchone_pgadmin(self):
                    row = orig_fetchone(self) if orig_fetchone else None
                    if row:
                        processed = _process_result(self, [row])
                        return processed[0] if processed else None
                    return None

                def traced_fetchmany_pgadmin(self, *args, **kwargs):
                    rows = orig_fetchmany(self, *args, **kwargs) if orig_fetchmany else []
                    return _process_result(self, rows)

                @property
                def traced_description(self):
                    desc = orig_desc_getter(self)
                    meta = get_cursor_state(self, "meta", [])
                    return _strip_description(desc, meta)

                def traced_ordered_description(self):
                    desc = orig_ordered_description(self)
                    meta = get_cursor_state(self, "meta", [])
                    return _strip_description(desc, meta)

                cls.execute = traced_execute_pgadmin
                if orig_fetchall: cls.fetchall = traced_fetchall_pgadmin
                if orig_fetchone: cls.fetchone = traced_fetchone_pgadmin
                if orig_fetchmany: cls.fetchmany = traced_fetchmany_pgadmin
                setattr(cls, "description", traced_description)
                if orig_ordered_description:
                    cls.ordered_description = traced_ordered_description

                log_debug(f"[hook] Patched {cls.__name__}")

            for t in targets:
                _patch_class_methods(t)

            import pgadmin.utils.driver.psycopg3.connection as pg_connection
            conn_cls = pg_connection.Connection
            if not getattr(conn_cls, "_nomai_instrumented", False):
                orig_execute_async = conn_cls.execute_async
                orig_async_fetchmany = conn_cls.async_fetchmany_2darray
                orig_get_column_info = conn_cls.get_column_info

                def traced_execute_async(self, query, params=None, formatted_exception_msg=True):
                    rid = current_request_id(None) or request_id_from_query(query)
                    set_cursor_state(self, "request_id", rid)
                    set_cursor_state(self, "meta", [])
                    set_cursor_state(self, "read_sequence", None)
                    set_cursor_state(self, "snapshot_bounds", None)

                    if rid and getattr(self, "conn", None):
                        safe_rid = str(rid).replace("'", "''")
                        try:
                            self.conn.execute(f"SET request_id = '{safe_rid}'")
                        except Exception as e:
                            log_debug(f"query_tool request_id error: {e}")

                    final_query = query
                    try:
                        final_query, meta_list = _rewrite_query_if_needed(query)
                        if meta_list:
                            set_cursor_state(self, "meta", meta_list)
                            set_cursor_state(self, "read_sequence", next_read_sequence())
                    except Exception as e:
                        log_debug(f"query_tool rewrite error: {e}")

                    return orig_execute_async(self, final_query, params, formatted_exception_msg)

                def traced_async_fetchmany_2darray(self, records=2000, formatted_exception_msg=False):
                    status, result = orig_async_fetchmany(self, records, formatted_exception_msg)
                    if status and result and get_cursor_state(self, "meta", []):
                        result = _process_result(self, result)
                    return status, result

                def traced_get_column_info(self):
                    info = orig_get_column_info(self)
                    meta = get_cursor_state(self, "meta", [])
                    return _strip_description(info, meta)

                conn_cls.execute_async = traced_execute_async
                conn_cls.async_fetchmany_2darray = traced_async_fetchmany_2darray
                conn_cls.get_column_info = traced_get_column_info
                conn_cls._nomai_instrumented = True

            log_debug("[hook] pgadmin psycopg3 cursors patched successfully")
            break

        except Exception as e:
            log_debug(f"[hook] patch_pgadmin_cursor failed: {e}")
            time.sleep(2)

threading.Thread(target=_try_patch_pgadmin_cursor, daemon=True).start()

# --------- hook Flask ---------
try:
    orig_flask_init = flask.Flask.__init__

    def hooked_flask_init(self, *args, **kwargs):
        orig_flask_init(self, *args, **kwargs)

        @self.before_request
        def _req_start():
            rid = (
                flask.request.headers.get("X-Request-ID")
                or flask.request.headers.get("X-Request-Id")
                or next_request_id()
            )
            flask.g.request_id = rid
            reset_read_sequence()
            log_debug(f"REQ_START {rid}")

        @self.after_request
        def _req_end(response):
            rid = getattr(flask.g, "request_id", "unknown")
            if rid != "unknown":
                response.headers['X-Request-ID'] = rid
                try:
                    api_id, route = _request_api_id()
                    user_id, identity_source = _request_user_identity(str(rid))
                    _emit_request_context_dedup({
                        "time": datetime.datetime.now().astimezone().isoformat(),
                        "rid": str(rid),
                        "method": flask.request.method.upper(),
                        "route": route,
                        "api_id": api_id,
                        "user_id": user_id,
                        "identity_source": identity_source,
                    })
                except Exception as e:
                    log_debug(f"Failed to collect request context: {e}")
                log_debug(f"REQ_END {rid}")
            return response

    flask.Flask.__init__ = hooked_flask_init
except Exception as e:
    log_debug(f"[hook] Flask hook failed: {e}")
