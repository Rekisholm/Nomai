# gunicorn_config.py
import os
import sys

# Ensure pythonpath
sys.path.insert(0, '/app/pythonpath')

print(f"[Gunicorn Master PID:{os.getpid()}] Starting Gunicorn configuration...")

# -- preload hook --
# This code runs in the master process before fork
try:
    import hook
    print(f"[Gunicorn Master PID:{os.getpid()}] Successfully imported and executed 'hook.py' in master process.")
except Exception as e:
    import traceback
    print(f"[Gunicorn Master PID:{os.getpid()}] FATAL: Error importing 'hook.py': {e}")
    traceback.print_exc()

def post_fork(server, worker):
    """
    Gunicorn hook, called after a worker process is forked.
    """
    worker_pid = os.getpid()
    print(f"[Gunicorn Worker PID:{worker_pid}] Worker process forked. Resetting per-worker state if necessary.")
    # If worker-specific counter reset or pool reconnection is needed later, do it here
    # For example: hook.reset_counter_for_worker()
    
print(f"[Gunicorn Master PID:{os.getpid()}] Gunicorn configuration finished.")