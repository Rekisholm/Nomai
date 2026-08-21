# sitecustomize.py
"""Load Airflow hooks"""
import sys
import os

def safe_init_hooks():
    """Initialize hooks safely"""
    if hasattr(sys, '_airflow_hook_initialized'):
        return
    sys._airflow_hook_initialized = True
    
    try:
        if os.environ.get("AIRFLOW_HOOK_DISABLED", "").lower() == "true":
            print("[sitecustomize] Hooks disabled by AIRFLOW_HOOK_DISABLED", 
                file=sys.stderr, flush=True)
            return       
        try:
            import airflow_hook
            airflow_hook.init_hooks()
        except Exception as e:
            print(f"[sitecustomize] [ERROR] Init error: {e}",
                  file=sys.stderr, flush=True)
            
    except Exception as e:
        pass  

safe_init_hooks()
