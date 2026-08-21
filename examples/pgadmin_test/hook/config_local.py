from config import *
import os

import sys
import os
try:
    # Ensure the current directory is in path
    sys.path.append(os.path.dirname(__file__))
    import hook
except Exception as e:
    print(f"Hook Load Error: {e}", file=sys.stderr)

# Debug mode
DEBUG = False

# App mode
SERVER_MODE = True

# Enable the test module
MODULE_BLACKLIST.remove('test')

# Log
CONSOLE_LOG_LEVEL = DEBUG
FILE_LOG_LEVEL = DEBUG

DEFAULT_SERVER = '0.0.0.0'

CONFIG_DATABASE_URI = os.environ.get('CONFIG_DATABASE_URI', '')