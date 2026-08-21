# airflow_hook.py
import psycopg2
import re
import threading
import ctypes
import os
from typing import Optional
import json
import datetime
from typing import Optional, List, Dict, Any
from airflow_hook_lib import (
    call_rewrite_sql, 
    is_lib_loaded, 
    UNSUPPORTED_REGEX
)

# ============ Configuration ============
HOOK_ENABLED = True
VERBOSE = True
SNAPSHOT_COLUMN_COUNT = 2

def set_hook_enabled(enabled: bool):
    global HOOK_ENABLED
    HOOK_ENABLED = enabled

def is_hook_enabled() -> bool:
    if os.environ.get("AIRFLOW_HOOK_DISABLED", "").lower() == "true":
        return False
    return HOOK_ENABLED

def log_debug(msg: str):
    import sys
    print(f"[airflow_hook] {msg}", file=sys.stderr, flush=True)


# ============ Global request ID management ============
_request_counter = 0
_request_lock = threading.Lock()
_context_storage = threading.local()

try:
    libc = ctypes.CDLL(None)
    #null_file = open("/dev/null", "wb", buffering=0)
    request_db_trace_file = open("/tmp/logs/request_db_read.log", "a", buffering=1)
except Exception as e:
    #null_file = None
    request_db_trace_file = None
    log_debug(f"Failed to open request_db_read.log: {e}")

try:
    request_context_file = open("/tmp/logs/request_context.log", "a", buffering=1)
except Exception as e:
    request_context_file = None
    log_debug(f"Failed to open request_context.log: {e}")

def _emit(line: str):
    if request_db_trace_file is None:
        return
    try:
        #null_file.write(line.encode("utf-8"))
        request_db_trace_file.write(line + "\n")
    except Exception as e:
        log_debug(f"Failed to write to request_db_trace.log: {e}")

def _emit_request_context(record: dict):
    if request_context_file is None:
        return
    try:
        request_context_file.write(json.dumps(record) + "\n")
    except Exception as e:
        log_debug(f"Failed to write to request_context.log: {e}")

import hashlib

def _request_user_identity(rid: str):
    """Prefer the Flask-Login user ID; otherwise use a session cookie digest; finally fall back to request:<rid>."""
    try:
        import flask
        from flask_login import current_user
        if current_user.is_authenticated:
            user_id = current_user.get_id()
            if user_id is not None:
                return f"user:{user_id}", "flask_login"
    except Exception as e:
        log_debug(f"Failed to read flask_login.current_user: {e}")
    try:
        import flask
        cookie_name = flask.current_app.config.get("SESSION_COOKIE_NAME", "session")
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
        import flask
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

def next_request_id() -> str:
    """Generate a globally unique request ID"""
    global _request_counter
    with _request_lock:
        _request_counter += 1
        pid = os.getpid()
        return f"{pid}.{_request_counter}"

def set_request_id(rid: str):
    _context_storage.request_id = rid
    _context_storage.read_sequence = 0

def get_request_id() -> Optional[str]:
    return getattr(_context_storage, "request_id", None)

def next_read_sequence() -> int:
    sequence = getattr(_context_storage, "read_sequence", 0)
    _context_storage.read_sequence = sequence + 1
    return sequence

# ============ psycopg2 Hook ============
class HookedCursor(psycopg2.extensions.cursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._in_hook = False  # Avoid recursive calls
        self._in_read_hook = False # Avoid recursive fetches
        self._current_meta: List[Dict[str, Any]] = [] 
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
    
    def execute(self, query, vars=None):
        # Avoid recursive calls
        if self._in_hook or not is_hook_enabled():
            self._current_meta = []
            self._current_read_sequence = None
            return super().execute(query, vars)
        
        self._in_hook = True
        try:
            return self._execute_with_rust_rewrite(query, vars)
        finally:
            self._in_hook = False
    
    def _execute_with_rust_rewrite(self, query, vars=None):
        
        # 1. Pre-check: process SELECT only
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

        #if UNSUPPORTED_REGEX.search(query_stripped):
        ##    self._current_meta = []
        #    return super().execute(query, vars)

        # 2. Call Rust for rewriting
        final_query = query
        self._current_meta = [] # Reset
        self._current_read_sequence = None
        
        try:
            new_sql, meta_list = call_rewrite_sql(query.encode('utf-8'))
            if new_sql and meta_list:
                final_query = new_sql
                self._current_meta = meta_list
                self._current_read_sequence = next_read_sequence()
        except Exception as e:
            log_debug(f"Rewriter error: {e}")
            self._current_meta = []
            self._current_read_sequence = None
        
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
                            "rid": rid,
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
        return cleaned_rows

_orig_connect = psycopg2.connect
def hooked_connect(*args, **kwargs):
    kwargs.setdefault("cursor_factory", HookedCursor)
    return _orig_connect(*args, **kwargs)
psycopg2.connect = hooked_connect


# ============ Airflow Webserver Hook ============
def hook_flask_webserver():
    """Hook Flask webserver request handling"""
    try:
        import sys
        from airflow.www.app import create_app as orig_create_app
        
        def hooked_create_app(*args, **kwargs):
            app = orig_create_app(*args, **kwargs)
            
            current_pid = os.getpid()
            log_debug(f"Installing Flask hooks (PID: {current_pid})...")
            
            import flask
            
            @app.before_request
            def inject_request_id():
                rid = next_request_id()
                set_request_id(rid)
                flask.g.request_id = rid
                path = flask.request.path
                #_emit(f"REQ_START {rid}")
                log_debug(f"Request {rid}: {path}")
            
            @app.after_request
            def log_request_end(response):
                rid = get_request_id()
                if rid:
                    response.headers['X-Request-ID'] = rid   # Add request_id to the response header
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
                    log_debug(f"[OK] Request {rid} completed")
                return response
            
            try:
                before_funcs = app.before_request_funcs.setdefault(None, [])
                if len(before_funcs) > 1:
                    our_hook = before_funcs.pop()
                    before_funcs.insert(0, our_hook)
                    log_debug(f"Reordered {len(before_funcs)} before_request hooks")
            except Exception as e:
                print(f"[airflow_hook] Failed to reorder hooks: {e}", 
                        file=sys.stderr, flush=True)
            
            log_debug("[OK] Flask hooks installed successfully")
            return app
        
        import airflow.www.app
        airflow.www.app.create_app = hooked_create_app
        log_debug("[OK] Patched airflow.www.app.create_app")
        
    except Exception as e:
        log_debug(f"[airflow_hook] [ERROR] Flask hook failed: {e}")

def init_hooks():
    """Initialize all hooks"""
    if hasattr(init_hooks, '_initialized'):
        return
    init_hooks._initialized = True
    
    try:
        import sys
        
        process_type = os.environ.get("AIRFLOW_PROCESS_TYPE", "")
        current_pid = os.getpid()
        
        if os.environ.get("AIRFLOW_HOOK_DISABLED", "").lower() == "true":
            log_debug("Hooks disabled by environment variable")
            set_hook_enabled(False)
            return
        
        log_debug(f"Initializing hooks (PID: {current_pid}, Type: {process_type or 'unknown'})")
        
        try:
            hook_flask_webserver()
        except Exception as e:
            log_debug(f"[airflow_hook] Warning: Flask hook failed: {e}")
        
        log_debug("Initialization complete")
              
    except Exception as e:
        try:
            log_debug(f"[airflow_hook] [ERROR] Fatal error: {e}")
            import traceback
            traceback.print_exc(file=sys.stderr)
        except:
            pass
