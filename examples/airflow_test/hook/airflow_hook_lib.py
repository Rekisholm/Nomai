# airflow_hook_lib.py
import ctypes
import os
import json
import re
from typing import Optional, Tuple, List, Dict, Any

# ============ Configuration constants ============
SO_PATH = os.environ.get("SQL_REWRITER_SO_PATH", "/opt/airflow/librust_sql_rewriter.so")

# Table configuration for primary-key injection
TABLE_CONFIG = [
    { "table": "alembic_version", "pk": "version_num" },
    { "table": "dag_pickle", "pk": "id" },
    { "table": "import_error", "pk": "id" },
    { "table": "job", "pk": "id" },
    { "table": "slot_pool", "pk": "id" },
    { "table": "chart", "pk": "id" },
    { "table": "known_event_type", "pk": "id" },
    { "table": "known_event", "pk": "id" },
    { "table": "xcom", "pk": "id" },
    { "table": "log", "pk": "id" },
    { "table": "dag_run", "pk": "id" },
    { "table": "sla_miss", "pk": "task_id" },
    { "table": "connection", "pk": "id" },
    { "table": "variable", "pk": "id" },
    { "table": "task_fail", "pk": "id" },
    { "table": "kube_resource_version", "pk": "one_row_id" },
    { "table": "kube_worker_uuid", "pk": "one_row_id" },
    { "table": "task_reschedule", "pk": "id" },
    { "table": "users", "pk": "id" },
    { "table": "dag", "pk": "dag_id" },
    { "table": "dag_tag", "pk": "name" },
    { "table": "task_instance", "pk": "task_id" },
    { "table": "rendered_task_instance_fields", "pk": "dag_id" },
    { "table": "dag_code", "pk": "fileloc_hash" },
    { "table": "serialized_dag", "pk": "dag_id" },
]

# ============ Precompiled regular expressions (performance optimization) ============
# Precompile regexes to avoid recompiling on each SQL execution
UNSUPPORTED_PATTERNS = [
    r"\bGROUP\s+BY\b",      # GROUP BY
    r"\bDISTINCT\b",        # DISTINCT
    r"\bHAVING\b",          # HAVING
    r"\bUNION\b",           # UNION
    r"\bCOUNT\s*\(",        # COUNT(...)
    r"\bSUM\s*\(",          # SUM(...)
    r"\bAVG\s*\(",          # AVG(...)
    r"\bMAX\s*\(",          # MAX(...)
    r"\bMIN\s*\(",          # MIN(...)
]
# Set the IGNORECASE flag at compile time
UNSUPPORTED_REGEX = re.compile("|".join(UNSUPPORTED_PATTERNS), re.IGNORECASE)


# ============ FFI definitions and loading ============
class RewriteResult(ctypes.Structure):
    _fields_ = [
        ("new_sql", ctypes.c_char_p),
        ("meta_json", ctypes.c_char_p),
        ("error_msg", ctypes.c_char_p),
    ]

_rust_lib = None

def _init_rust_lib():
    """Internal initialization function"""
    global _rust_lib
    try:
        if os.path.exists(SO_PATH):
            lib = ctypes.CDLL(SO_PATH)
            
            lib.init_config.argtypes = [ctypes.c_char_p]
            lib.init_config.restype = ctypes.c_int

            lib.rewrite_sql.argtypes = [ctypes.c_char_p]
            lib.rewrite_sql.restype = RewriteResult

            lib.free_rewrite_result.argtypes = [RewriteResult]
            lib.free_rewrite_result.restype = None
            
            # Initialize configuration
            cfg_json = json.dumps(TABLE_CONFIG).encode('utf-8')
            if lib.init_config(cfg_json) != 0:
                print("[RustHook] Failed to init Rust config!")
                return None
            
            print(f"[RustHook] Loaded Rust library from {SO_PATH}")
            return lib
        else:
            print(f"[RustHook] Warning: .so file not found at {SO_PATH}")
            return None
    except Exception as e:
        print(f"[RustHook] FFI Load Error: {e}")
        return None

# Singleton loading
_rust_lib = _init_rust_lib()

def is_lib_loaded() -> bool:
    return _rust_lib is not None

def call_rewrite_sql(sql_bytes: bytes) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Wrap the Rust call.
    Returns: (new_sql_string, meta_list)
    Return (None, []) on error or when rewrite is unnecessary
    """
    if not _rust_lib:
        return None, []

    try:
        res = _rust_lib.rewrite_sql(sql_bytes)
        
        new_sql = None
        meta = []

        if res.error_msg:
            # Optional: print Rust parser errors here
            # err = res.error_msg.decode('utf-8')
            # print(f"Rust rewrite error: {err}")
            pass
        elif res.new_sql:
            new_sql = res.new_sql.decode('utf-8')
            if res.meta_json:
                meta = json.loads(res.meta_json.decode('utf-8'))
        
        _rust_lib.free_rewrite_result(res)
        return new_sql, meta
        
    except Exception:
        return None, []