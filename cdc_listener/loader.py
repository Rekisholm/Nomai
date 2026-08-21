import json
import os

CVE_NAME = os.environ.get("CVE_NAME", "admidio5.0.5_full")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, f"config_{CVE_NAME}.json")

def load_config(config_path=CONFIG_PATH):
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file '{config_path}' not found.")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data
