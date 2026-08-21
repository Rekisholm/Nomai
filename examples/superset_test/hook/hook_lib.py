# hook_lib.py
import ctypes
import os
import json
import re
from typing import Optional, Tuple, List, Dict, Any

# ============ Configuration constants ============
SO_PATH = os.environ.get("SQL_REWRITER_SO_PATH", "/app/librust_sql_rewriter.so")

TABLE_CONFIG = [
    { "table": "ab_user_role", "pk": "id" },
    { "table": "ab_user", "pk": "id" },
    { "table": "ab_register_user", "pk": "id" },
    { "table": "ab_permission", "pk": "id" },
    { "table": "ab_permission_view", "pk": "id" },
    { "table": "ab_view_menu", "pk": "id" },
    { "table": "ab_role", "pk": "id" },
    { "table": "ab_permission_view_role", "pk": "id" },
    { "table": "url", "pk": "id" },
    { "table": "alembic_version", "pk": "version_num" },
    { "table": "dashboard_slices", "pk": "id" },
    { "table": "css_templates", "pk": "id" },
    { "table": "favstar", "pk": "id" },
    { "table": "dashboard_user", "pk": "id" },
    { "table": "slice_user", "pk": "id" },
    { "table": "access_request", "pk": "id" },
    { "table": "logs", "pk": "id" },
    { "table": "keyvalue", "pk": "id" },
    { "table": "annotation_layer", "pk": "id" },
    { "table": "user_attribute", "pk": "id" },
    { "table": "annotation", "pk": "id" },
    { "table": "sqlatable_user", "pk": "id" },
    { "table": "druiddatasource_user", "pk": "id" },
    { "table": "tag", "pk": "id" },
    { "table": "tagged_object", "pk": "id" },
    { "table": "table_schema", "pk": "id" },
    { "table": "rls_filter_roles", "pk": "id" },
    { "table": "rls_filter_tables", "pk": "id" },
    { "table": "alert_logs", "pk": "id" },
    { "table": "alert_owner", "pk": "id" },
    { "table": "sql_observations", "pk": "id" },
    { "table": "cache_keys", "pk": "id" },
    { "table": "row_level_security_filters", "pk": "id" },
    { "table": "sql_metrics", "pk": "id" },
    { "table": "clusters", "pk": "id" },
    { "table": "metrics", "pk": "id" },
    { "table": "dashboard_email_schedules", "pk": "id" },
    { "table": "slice_email_schedules", "pk": "id" },
    { "table": "alerts", "pk": "id" },
    { "table": "saved_query", "pk": "id" },
    { "table": "report_recipient", "pk": "id" },
    { "table": "report_schedule_user", "pk": "id" },
    { "table": "dynamic_plugin", "pk": "id" },
    { "table": "dashboard_roles", "pk": "id" },
    { "table": "report_execution_log", "pk": "id" },
    { "table": "query", "pk": "id" },
    { "table": "filter_sets", "pk": "id" },
    { "table": "report_schedule", "pk": "id" },
    { "table": "tab_state", "pk": "id" },
    { "table": "dashboards", "pk": "id" },
    { "table": "datasources", "pk": "id" },
    { "table": "dbs", "pk": "id" },
    { "table": "slices", "pk": "id" },
    { "table": "tables", "pk": "id" },
    { "table": "key_value", "pk": "id" },
    { "table": "sl_tables", "pk": "id" },
    { "table": "sl_datasets", "pk": "id" },
    { "table": "sl_table_columns", "pk": "table_id" },
    { "table": "sl_dataset_columns", "pk": "dataset_id" },
    { "table": "sl_dataset_tables", "pk": "dataset_id" },
    { "table": "sl_dataset_users", "pk": "dataset_id" },
    { "table": "table_columns", "pk": "id" },
    { "table": "columns", "pk": "id" },
    { "table": "sl_columns", "pk": "id" }
]
# ============ Precompiled regular expressions (performance optimization) ============
# Precompile regexes to avoid recompiling on each SQL execution
UNSUPPORTED_PATTERNS = [
    r"\bGROUP\s+BY\b",      # GROUP BY
    r"\bDISTINCT\b",        # DISTINCT
    r"\bHAVING\b",          # HAVING
    r"\bUNION\b",           # UNION
    r"\bEXISTS\b",       # EXISTS
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