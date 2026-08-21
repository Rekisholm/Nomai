import base64
import os
import random
import re
import string
import time
import xml.etree.ElementTree as ET

import requests
from gevent.lock import Semaphore
from locust import HttpUser, between, events, task

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from request_logger import RequestLogger

REQUEST_LOGGER = RequestLogger(Path(__file__).resolve().parent / "request_log")
REQUEST_LOGGER.install(events)


# ========== Configuration ==========
HOST = os.getenv("GEOSERVER_HOST", "http://localhost")
PORT = int(os.getenv("GEOSERVER_PORT", "8080"))
BASE_URL = os.getenv("GEOSERVER_BASE_URL", f"{HOST}:{PORT}").rstrip("/")
USERNAME = os.getenv("GEOSERVER_USERNAME", "admin")
PASSWORD = os.getenv("GEOSERVER_PASSWORD", "geoserver")
TIMEOUT = int(os.getenv("GEOSERVER_TIMEOUT", "20"))

WORKSPACE = os.getenv("GEOSERVER_WORKSPACE", "vulhub")
LAYER = os.getenv("GEOSERVER_LAYER", "example")
NAMESPACE = f"{WORKSPACE}:{LAYER}"
SRS = os.getenv("GEOSERVER_SRS", "EPSG:404000")

BASIC_AUTH = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
WRITE_LOCK = Semaphore()

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


def random_str(length=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def normalize_url_path(path):
    path = re.sub(r"/styles/[^/?]+", "/styles/:name", path)
    path = re.sub(r"name=[^&]+", "name=:name", path)
    path = re.sub(r"feature_id=[^&]+", "feature_id=:fid", path)
    path = re.sub(r"CQL_FILTER=[^&]+", "CQL_FILTER=:filter", path)
    return path


def xml_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def wfs_success(text):
    return "<wfs:SUCCESS/>" in text or "<wfs:SUCCESS />" in text or "<SUCCESS/>" in text


def parse_feature_ids(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    feature_ids = []
    for elem in root.iter():
        fid = elem.attrib.get("fid") or elem.attrib.get("{http://www.opengis.net/gml}id")
        if fid:
            feature_ids.append(fid)
    return feature_ids


class GeoServerUser(HttpUser):
    wait_time = between(1, 3)
    host = BASE_URL

    def on_start(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "locust-geoserver/1.0"})
        self._check_geoserver()

    def _send_request(self, method, url, name=None, expected_statuses=None, **kwargs):
        full_url = f"{BASE_URL}{url}" if url.startswith("/") else url
        stat_name = name if name else normalize_url_path(url)
        expected_statuses = expected_statuses or set(range(200, 400))
        kwargs.setdefault("timeout", TIMEOUT)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Authorization", BASIC_AUTH)
        kwargs["headers"] = headers
        start_time = time.time()

        try:
            resp = self.session.request(method, full_url, **kwargs)
            REQUEST_LOGGER.write_response(resp, start_time, method, url)
            response_time = (time.time() - start_time) * 1000
            exception = None
            if resp.status_code not in expected_statuses:
                exception = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=len(resp.content),
                context={},
                exception=exception,
            )
            return resp
        except Exception as exc:
            response_time = (time.time() - start_time) * 1000
            events.request.fire(
                request_type=method.upper(),
                name=stat_name,
                response_time=response_time,
                response_length=0,
                context={},
                exception=exc,
            )
            raise

    def _check_geoserver(self):
        resp = self._send_request("GET", "/geoserver/rest/about/version.json", headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise Exception(f"GeoServer REST is unavailable: {resp.status_code}")

    def _wfs_transaction(self, xml, name, expected_success=True, params=None):
        resp = self._send_request(
            "POST",
            "/geoserver/wfs",
            name=name,
            params=params or {},
            data=xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
        )
        if expected_success and not wfs_success(resp.text):
            events.request.fire(
                request_type="WFS",
                name=f"{name} success-check",
                response_time=0,
                response_length=len(resp.content),
                context={},
                exception=Exception(f"WFS transaction failed: {resp.text[:200]}"),
            )
        return resp

    def _create_feature(self, feature_name):
        escaped_name = xml_escape(feature_name)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:gml="http://www.opengis.net/gml"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Insert>
    <{WORKSPACE}:{LAYER}>
      <{WORKSPACE}:name>{escaped_name}</{WORKSPACE}:name>
    </{WORKSPACE}:{LAYER}>
  </wfs:Insert>
</wfs:Transaction>"""
        return self._wfs_transaction(xml, "/geoserver/wfs?type=createFeature", params={"type": "createFeature", "name": feature_name})

    def _find_feature_id_by_name(self, feature_name):
        escaped_name = xml_escape(feature_name)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:GetFeature service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}"
 outputFormat="GML2">
  <wfs:Query typeName="{WORKSPACE}:{LAYER}">
    <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
      <ogc:PropertyIsEqualTo>
        <ogc:PropertyName>name</ogc:PropertyName>
        <ogc:Literal>{escaped_name}</ogc:Literal>
      </ogc:PropertyIsEqualTo>
    </ogc:Filter>
  </wfs:Query>
</wfs:GetFeature>"""
        resp = self._send_request(
            "POST",
            "/geoserver/wfs",
            name="/geoserver/wfs?type=getFeatureByName",
            params={"type": "getFeatureByName"},
            data=xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
        )
        ids = parse_feature_ids(resp.content)
        return ids[0] if ids else None

    def _update_feature(self, feature_id, new_name):
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Update typeName="{WORKSPACE}:{LAYER}">
    <wfs:Property>
      <wfs:Name>name</wfs:Name>
      <wfs:Value>{xml_escape(new_name)}</wfs:Value>
    </wfs:Property>
    <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
      <ogc:FeatureId fid="{xml_escape(feature_id)}"/>
    </ogc:Filter>
  </wfs:Update>
</wfs:Transaction>"""
        return self._wfs_transaction(
            xml,
            "/geoserver/wfs?type=updateFeature",
            params={"type": "updateFeature", "feature_id": feature_id},
        )

    def _delete_feature(self, feature_id):
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Delete typeName="{WORKSPACE}:{LAYER}">
    <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
      <ogc:FeatureId fid="{xml_escape(feature_id)}"/>
    </ogc:Filter>
  </wfs:Delete>
</wfs:Transaction>"""
        return self._wfs_transaction(
            xml,
            "/geoserver/wfs?type=deleteFeature",
            params={"type": "deleteFeature", "feature_id": feature_id},
        )

    def _create_style(self, style_name):
        sld_body = SLD_CONTENT_TEMPLATE.format(name=style_name, title=f"Auto Style {style_name}")
        return self._send_request(
            "POST",
            "/geoserver/rest/styles",
            params={"name": style_name},
            name="/geoserver/rest/styles?name=:name",
            data=sld_body.encode("utf-8"),
            headers={"Content-Type": "application/vnd.ogc.sld+xml"},
            expected_statuses={201},
        )

    def _update_style(self, style_name):
        sld_body = SLD_CONTENT_TEMPLATE.format(name=style_name, title=f"Updated {style_name}")
        return self._send_request(
            "PUT",
            f"/geoserver/rest/styles/{style_name}",
            name="/geoserver/rest/styles/:name",
            data=sld_body.encode("utf-8"),
            headers={"Content-Type": "application/vnd.ogc.sld+xml"},
        )

    def _delete_style(self, style_name):
        return self._send_request(
            "DELETE",
            f"/geoserver/rest/styles/{style_name}",
            name="/geoserver/rest/styles/:name",
            expected_statuses={200, 404},
        )

    @task(4)
    def rest_read_flow(self):
        self._send_request("GET", "/geoserver/rest/about/version.json", headers={"Accept": "application/json"})
        self._send_request("GET", "/geoserver/rest/workspaces.json", headers={"Accept": "application/json"})
        self._send_request("GET", f"/geoserver/rest/workspaces/{WORKSPACE}.json", headers={"Accept": "application/json"})
        self._send_request("GET", f"/geoserver/rest/workspaces/{WORKSPACE}/datastores.json", headers={"Accept": "application/json"})
        self._send_request("GET", f"/geoserver/rest/layers/{NAMESPACE}.json", headers={"Accept": "application/json"})
        self._send_request("GET", "/geoserver/rest/styles.json", headers={"Accept": "application/json"})

    @task(3)
    def ogc_read_flow(self):
        self._send_request(
            "GET",
            "/geoserver/ows",
            name="/geoserver/ows?service=wfs&request=GetCapabilities",
            params={"service": "wfs", "version": "1.0.0", "request": "GetCapabilities"},
        )
        self._send_request(
            "GET",
            "/geoserver/ows",
            name="/geoserver/ows?service=wfs&request=DescribeFeatureType",
            params={"service": "wfs", "version": "1.0.0", "request": "DescribeFeatureType", "typeName": NAMESPACE},
        )
        self._send_request(
            "GET",
            "/geoserver/ows",
            name="/geoserver/ows?service=wfs&request=GetFeature",
            params={"service": "wfs", "version": "1.0.0", "request": "GetFeature", "typeName": NAMESPACE, "maxFeatures": "5"},
        )
        self._send_request(
            "GET",
            f"/geoserver/{WORKSPACE}/wms",
            name="/geoserver/:workspace/wms?request=GetCapabilities",
            params={"service": "WMS", "version": "1.1.1", "request": "GetCapabilities"},
        )
        self._send_request(
            "GET",
            f"/geoserver/{WORKSPACE}/wms",
            name="/geoserver/:workspace/wms?request=GetMap",
            params={
                "service": "WMS",
                "version": "1.1.1",
                "request": "GetMap",
                "layers": NAMESPACE,
                "styles": "",
                "bbox": "-180,-90,180,90",
                "width": "256",
                "height": "128",
                "srs": SRS,
                "format": "image/png",
            },
            expected_statuses={200, 400},
        )

    @task(2)
    def feature_lifecycle(self):
        with WRITE_LOCK:
            feature_name = f"locust_{random_str(10)}"
            feature_id = None
            self._create_feature(feature_name)
            time.sleep(0.2)
            feature_id = self._find_feature_id_by_name(feature_name)
            if feature_id:
                self._update_feature(feature_id, f"updated_{feature_name}")
                self._delete_feature(feature_id)

    @task(1)
    def style_lifecycle(self):
        with WRITE_LOCK:
            style_name = f"test_{random_str(10)}"
            self._create_style(style_name)
            self._update_style(style_name)
            self._delete_style(style_name)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=== GeoServer concurrent test started ===")
    print(f"Target URL: {BASE_URL}")
    print(f"Workspace/layer: {NAMESPACE}")
