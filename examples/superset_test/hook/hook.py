# hook.py
import os
import sys
import psycopg2
import flask
import ctypes
import threading
import datetime
import hashlib
import json
import importlib
import time
import weakref
import uuid
from typing import Optional, List, Union, Any, Tuple
from hook_lib import (
    call_rewrite_sql, 
    is_lib_loaded, 
    UNSUPPORTED_REGEX
)

VERBOSE = os.environ.get("SUPERSET_HOOK_VERBOSE", "false").lower() == "true"
SNAPSHOT_COLUMN_COUNT = 2
def log_debug(message: str):
    if VERBOSE:
        print(f"[HOOK] {message}", file=sys.stderr, flush=True)

try:
    libc = ctypes.CDLL(None)
    file_name = "/tmp/logs/request_db_read.log"
    #file_name = "/dev/null"
    request_db_trace_file = open(file_name, "a", buffering=1)
except Exception as e:
    request_db_trace_file = None
    log_debug(f"Failed to open request_db_read.log: {e}")

try:
    request_context_file = open(
        "/tmp/logs/request_context.log", "a", buffering=1
    )
except Exception as e:
    request_context_file = None
    log_debug(f"Failed to open request_context.log: {e}")

def _emit(line: str):
    if request_db_trace_file is None:
        return
    try:
        request_db_trace_file.write(line + "\n")
    except Exception as e:
        log_debug(f"Failed to write to request_db_trace.log: {e}")


def _emit_request_context(record: dict):
    if request_context_file is None:
        return
    try:
        request_context_file.write(json.dumps(record) + "\n")
    except Exception as e:
        log_debug(f"Failed to write request_context.log: {e}")


def _request_user_identity(rid: str) -> Tuple[str, str]:
    """Prefer Flask-Login user id; otherwise use a non-reversible session id."""
    try:
        from flask_login import current_user

        if current_user.is_authenticated:
            user_id = current_user.get_id()
            if user_id is not None:
                return f"user:{user_id}", "flask_login"
    except Exception as e:
        log_debug(f"Failed to read flask_login.current_user: {e}")

    cookie_name = flask.current_app.config.get("SESSION_COOKIE_NAME", "session")
    cookie = flask.request.cookies.get(cookie_name)
    if cookie:
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:24]
        return f"session:{digest}", "session_cookie_sha256"
    return f"request:{rid}", "request_id"


def _request_api_id() -> Tuple[str, str]:
    method = flask.request.method.upper()
    route = (
        flask.request.url_rule.rule
        if flask.request.url_rule is not None
        else flask.request.path
    )
    return f"{method} {route}", route

# --- Request ID management ---

_local = threading.local()
def next_request_id() -> str:
    return str(uuid.uuid4())

def set_request_id(rid: str):
    _local.request_id = rid
    _local.read_sequence = 0

def get_request_id() -> Optional[str]:
    # Check whether we are in a Flask request context; keep the original logic
    if not flask.has_request_context():
        return None
    return getattr(_local, "request_id", None)

def next_read_sequence() -> int:
    sequence = getattr(_local, "read_sequence", 0)
    _local.read_sequence = sequence + 1
    return sequence

_conn_last_rid = weakref.WeakKeyDictionary()

# --- Database hook ---
class HookedCursor(psycopg2.extensions.cursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._in_hook = False  # Avoid recursive calls
        self._in_read_hook = False # Avoid recursive fetches
        self._current_meta = []
        self._current_read_sequence = None

    @property
    def description(self):
        # Hide metadata for injected columns
        orig_desc = psycopg2.extensions.cursor.description.__get__(self)
        if not orig_desc or not self._current_meta:
            return orig_desc
        num_injected = len(self._current_meta) + SNAPSHOT_COLUMN_COUNT
        if num_injected >= len(orig_desc):
            return orig_desc
        return orig_desc[:-num_injected]
    
    def execute(self, query: str, vars: Optional[Union[Tuple, dict]] = None) -> Any:
        if self._in_hook:
            return super().execute(query, vars)
        
        self._in_hook = True
        try:
            return self._execute_with_rust_rewrite(query, vars)
        finally:
            self._in_hook = False
    
    def _execute_with_rust_rewrite(self, query, vars=None):

        # 1. Pre-check: distinguish SELECT statements
        query_stripped = query.strip()
        
        # For non-SELECT statements, inject request ID and execute directly
        if not query_stripped[:6].upper() == "SELECT":
            self._current_meta = []
            self._current_read_sequence = None
            rid = get_request_id()
            # Inject request_id into the Postgres session for DB-side log correlation
            if rid:
                try:
                    conn = self.connection
                    if conn.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
                        super().execute("SET LOCAL request_id = %s;", (rid,))
                except:
                    pass
            return super().execute(query, vars)

        # 2. Call Rust for rewriting
        final_query = query
        self._current_meta = [] # Reset
        self._current_read_sequence = None
        
        try:
            #start_time = time.time()
            new_sql, meta_list = call_rewrite_sql(query.encode('utf-8'))
            #end_time = time.time()
            #log_debug(f"SQL rewriter time: {1000*(end_time - start_time)}ms")
            if new_sql and meta_list:
                final_query = new_sql
                self._current_meta = meta_list
                self._current_read_sequence = next_read_sequence()
        except Exception as e:
            log_debug(f"Rewriter error: {e}")
            self._current_meta = []
        
        # 3. Execution (possibly rewritten or original)
        # Note: if vars exists, adding columns does not affect var matching because columns are appended to the SELECT list
        return super().execute(final_query, vars)

    def fetchall(self):
        if self._in_read_hook:
            return super().fetchall()
        self._in_read_hook = True
        try:
            rows = super().fetchall()
        finally:
            self._in_read_hook = False
        return self._process_result(rows)

    def fetchone(self):
        if self._in_read_hook:
            return super().fetchone()
        self._in_read_hook = True
        try:
            row = super().fetchone()
        finally:
            self._in_read_hook = False
        if row is None: return None
        processed = self._process_result([row])
        return processed[0] if processed else None

    def fetchmany(self, size=None):
        if self._in_read_hook:
            return super().fetchmany(size)
        self._in_read_hook = True
        try:
            rows = super().fetchmany(size)
        finally:
            self._in_read_hook = False
        return self._process_result(rows)

    def _process_result(self, rows):
        """
        Core logic:
        1. Extract injected primary-key columns for logging.
        2. Strip these columns from result rows so the application layer remains unaffected.
        """
        if not rows or not self._current_meta:
            return rows
        #start_time = time.time()
        num_pk_columns = len(self._current_meta)
        num_injected = num_pk_columns + SNAPSHOT_COLUMN_COUNT
        rid = get_request_id() or "unknown"
        
        # 1. Extract IDs and record logs
        if rid != "unknown":
            try:
                logs = []
                xmin_values = {int(row[-2]) for row in rows if row[-2] is not None}
                xmax_values = {int(row[-1]) for row in rows if row[-1] is not None}
                if len(xmin_values) != 1 or len(xmax_values) != 1:
                    raise ValueError(
                        f"inconsistent snapshot bounds: xmin={xmin_values}, xmax={xmax_values}"
                    )
                xmin = next(iter(xmin_values))
                xmax = next(iter(xmax_values))
                # Metadata structure: [{'table': 't', 'pk_col': 'id', ...}, ...]
                # PK columns are located before the snapshot boundary at the result tail.
                
                for i, meta in enumerate(self._current_meta):
                    col_index = -num_injected + i
                    
                    # Extract all non-null values in this column and deduplicate them
                    pks = {row[col_index] for row in rows if row[col_index] is not None}
                    
                    if pks:
                        logs.append({
                            "time": datetime.datetime.now().astimezone().isoformat(),
                            "event": "select",
                            "rid": str(rid),
                            "tbn": meta['table'],
                            "pks": list(pks),
                            "seq": self._current_read_sequence,
                            "xmin": xmin,
                            "xmax": xmax,
                        })
                for l in logs:
                    _emit(json.dumps(l))
            except Exception as e:
                log_debug(f"Error extracting IDs: {e}")

        # 2. Strip injected columns and restore the data
        cleaned_rows = [row[:-num_injected] for row in rows]
        #end_time = time.time()
        #log_debug(f"Row processing time: {1000*(end_time - start_time)}ms")
        return cleaned_rows



_orig_connect = psycopg2.connect
def traced_connect(*args, **kwargs):
    kwargs.setdefault("cursor_factory", HookedCursor)
    return _orig_connect(*args, **kwargs)
psycopg2.connect = traced_connect
log_debug("[OK] Patched psycopg2.connect to use HookedCursor.")

# ==========================================================
# ===== Final solution: delayed import and replacement at the last moment =====
# ==========================================================
log_debug("Preparing to patch 'superset.app.create_app' using delayed import...")

try:
    # 1. Define our own create_app function directly
    def hooked_create_app(*args, **kwargs):
        log_debug("hooked_create_app has been called! Now importing and running original create_app...")
        
        # 2. Import and fetch the original function only when our function is called
        superset_app_module = importlib.import_module("superset.app")
        original_create_app = getattr(superset_app_module, "_unhooked_create_app")
        
        # 3. Call the original function to create the app
        app = original_create_app(*args, **kwargs)
        log_debug(f"Original app created: {app.name}. Now installing Flask hooks...")

        # 4. Register hooks; this logic is unchanged
        @app.before_request
        def inject_request_id():
            rid = next_request_id(); set_request_id(rid); #flask.g.request_id = rid; path = flask.request.path; log_debug(f"Request {rid}: {path}")
        
        @app.after_request
        def log_request_end(response):
            rid = get_request_id()
            if rid:
                response.headers['X-Request-ID'] = str(rid)
                try:
                    api_id, route = _request_api_id()
                    user_id, identity_source = _request_user_identity(str(rid))
                    _emit_request_context({
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
            return response

        try:
            before_funcs = app.before_request_funcs.setdefault(None, [])
            if len(before_funcs) > 1: our_hook = before_funcs.pop(); before_funcs.insert(0, our_hook); #log_debug(f"Reordered {len(before_funcs)} before_request hooks.")
        except Exception as e: log_debug(f"Failed to reorder hooks: {e}")
            
        log_debug("[OK] Flask hooks installed successfully.")
        return app

    # 5. At module load time, do not import superset.app; treat it directly as a module object
    # This does not trigger code execution inside the module
    sys.modules['superset.app'] = importlib.import_module('superset.app')
    
    # 6. Save the original function under a new name and replace it with our function
    # This is the core of the monkey patch
    setattr(sys.modules['superset.app'], '_unhooked_create_app', sys.modules['superset.app'].create_app)
    setattr(sys.modules['superset.app'], 'create_app', hooked_create_app)
    
    log_debug("[OK] Successfully patched 'superset.app.create_app' in sys.modules.")

except Exception as e:
    log_debug(f"FATAL: An unexpected error occurred during the final hook setup: {e}")
    import traceback
    traceback.print_exc()
