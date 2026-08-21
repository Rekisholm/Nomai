# hook_lib.py
import ctypes
import os
import json
import re
from typing import Optional, Tuple, List, Dict, Any

# ============ Configuration constants ============
SO_PATH = os.environ.get("SQL_REWRITER_SO_PATH", "/tmp/librust_sql_rewriter.so")

# Table configuration for primary-key injection
TABLE_CONFIG = [
    { "table": "message", "pk": "message_id" },
    { "table": "alembic_version", "pk": "version_num" },
    { "table": "version", "pk": "name" },
    { "table": "setting", "pk": "user_id" },
    { "table": "role", "pk": "id" },
    { "table": "servergroup", "pk": "id" },
    { "table": "module_preference", "pk": "id" },
    { "table": "preference_category", "pk": "id" },
    { "table": "preferences", "pk": "id" },
    { "table": "user_preferences", "pk": "pid" },
    { "table": "debugger_function_arguments", "pk": "server_id" },
    { "table": "keys", "pk": "name" },
    { "table": "query_history", "pk": "srno" },
    { "table": "database", "pk": "id" },
    { "table": "macros", "pk": "id" },
    { "table": "user_macros", "pk": "mid" },
    { "table": "user", "pk": "id" },
    { "table": "user_mfa", "pk": "user_id" },
    { "table": "process", "pk": "pid" },
    { "table": "server", "pk": "id" },
    { "table": "sharedserver", "pk": "id" },
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
                print("[FFI_hook] Failed to init Rust config!")
                return None
            
            print("[FFI_hook] Loaded Rust library from " + SO_PATH)
            return lib
        else:
            print("[FFI_hook] Warning: .so file not found at " + SO_PATH)
            return None
    except Exception as e:
        print("[FFI_hook] FFI Load Error: " + e)
        return None

# Singleton loading
_rust_lib = _init_rust_lib()

# Cache configuration
CACHE_MAX_SIZE = 1000  # Maximum cache entries

# Cache dictionary for processed SQL and its results
# Use OrderedDict to implement an LRU cache
from collections import OrderedDict
_sql_cache = OrderedDict()

def is_lib_loaded() -> bool:
    return _rust_lib is not None

def call_rewrite_sql(sql_bytes: bytes) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Wrap the Rust call.
    Returns: (new_sql_string, meta_list)
    Return (None, []) on error or when rewrite is unnecessary
    """
    # Check whether the same sql_bytes exists in the cache
    if sql_bytes in _sql_cache:
        # Move the accessed entry to the end and mark it as recently used
        result = _sql_cache.pop(sql_bytes)
        _sql_cache[sql_bytes] = result
        return result
        
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
        
        # Store the result in the cache
        result = (new_sql, meta)
        
        # Check cache size and evict the least recently used entry if it exceeds the limit
        if len(_sql_cache) >= CACHE_MAX_SIZE:
            _sql_cache.popitem(last=False)  # Remove the least recently used entry
        
        _sql_cache[sql_bytes] = result
        return result
        
    except Exception:
        return None, []