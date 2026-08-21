#!/usr/bin/env python3
"""Artificial GeoServer provenance chain.

This script intentionally builds a DB-backed request chain instead of replaying
the original SQL injection.  The goal is to validate transaction-id provenance:

1. WFS Insert creates a private setup row. This step is not gold because this
   GeoServer path may not bind request_id to the insert CDC event.
2. WFS Update reads that row and writes example[name=marker_v2].
3. WFS GetFeature reads marker_v2 and becomes the root request.

Only update and final read print "X-Request-Id" and become gold. Setup and
cleanup print "RID" so they are not included in attack_rids.json.
"""

from __future__ import annotations

import os
import random
import string
import xml.etree.ElementTree as ET

import requests


BASE_URL = os.getenv("GEOSERVER_BASE_URL", "http://localhost:8080").rstrip("/")
USERNAME = os.getenv("GEOSERVER_USERNAME", "admin")
PASSWORD = os.getenv("GEOSERVER_PASSWORD", "geoserver")
WORKSPACE = os.getenv("GEOSERVER_WORKSPACE", "vulhub")
LAYER = os.getenv("GEOSERVER_LAYER", "example")
TIMEOUT = int(os.getenv("GEOSERVER_TIMEOUT", "30"))


def request_id(resp: requests.Response) -> str | None:
    return resp.headers.get("X-Request-Id") or resp.headers.get("X-Request-ID")


def log_response(label: str, resp: requests.Response, capture: bool = True) -> None:
    req = resp.request
    if capture:
        print(
            f"{label}: {req.method} {req.path_url} -> {resp.status_code} "
            f"X-Request-Id: {request_id(resp)}"
        )
    else:
        print(
            f"{label}: {req.method} {req.path_url} status={resp.status_code} "
            f"RID: {request_id(resp)}"
        )


def random_suffix(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def wfs_success(text: str) -> bool:
    return "<wfs:SUCCESS/>" in text or "<wfs:SUCCESS />" in text or "<SUCCESS/>" in text


def parse_feature_ids(content: bytes) -> list[str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    result: list[str] = []
    for elem in root.iter():
        fid = elem.attrib.get("fid") or elem.attrib.get("{http://www.opengis.net/gml}id")
        if fid:
            result.append(fid)
    return result


class GeoServerChain:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (USERNAME, PASSWORD)

    def post_wfs(self, xml: str) -> requests.Response:
        return self.session.post(
            f"{BASE_URL}/geoserver/wfs",
            data=xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            timeout=TIMEOUT,
        )

    def insert_feature(self, name: str) -> requests.Response:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Insert>
    <{WORKSPACE}:{LAYER}>
      <{WORKSPACE}:name>{xml_escape(name)}</{WORKSPACE}:name>
    </{WORKSPACE}:{LAYER}>
  </wfs:Insert>
</wfs:Transaction>"""
        return self.post_wfs(xml)

    def get_feature_by_name(self, name: str) -> requests.Response:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:GetFeature service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}"
 outputFormat="GML2">
  <wfs:Query typeName="{WORKSPACE}:{LAYER}">
    <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
      <ogc:PropertyIsEqualTo>
        <ogc:PropertyName>name</ogc:PropertyName>
        <ogc:Literal>{xml_escape(name)}</ogc:Literal>
      </ogc:PropertyIsEqualTo>
    </ogc:Filter>
  </wfs:Query>
</wfs:GetFeature>"""
        return self.post_wfs(xml)

    def update_feature(self, fid: str, new_name: str) -> requests.Response:
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
      <ogc:FeatureId fid="{xml_escape(fid)}"/>
    </ogc:Filter>
  </wfs:Update>
</wfs:Transaction>"""
        return self.post_wfs(xml)

    def delete_feature(self, fid: str) -> requests.Response:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:Transaction service="WFS" version="1.0.0"
 xmlns:wfs="http://www.opengis.net/wfs"
 xmlns:{WORKSPACE}="http://{WORKSPACE}">
  <wfs:Delete typeName="{WORKSPACE}:{LAYER}">
    <ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">
      <ogc:FeatureId fid="{xml_escape(fid)}"/>
    </ogc:Filter>
  </wfs:Delete>
</wfs:Transaction>"""
        return self.post_wfs(xml)


def main() -> None:
    client = GeoServerChain()
    marker = f"attack_chain_{random_suffix()}"
    updated_marker = f"{marker}_updated"
    fid: str | None = None

    # Setup: write the initial row. It is intentionally not part of gold.
    resp = client.insert_feature(marker)
    log_response("Setup insert feature", resp, capture=False)
    if resp.status_code >= 400 or not wfs_success(resp.text):
        raise RuntimeError(f"insert failed: {resp.status_code}\n{resp.text[:500]}")

    # Resolve the feature id. This is an implementation detail, not gold.
    resp = client.get_feature_by_name(marker)
    log_response("Resolve inserted feature", resp, capture=False)
    ids = parse_feature_ids(resp.content)
    if not ids:
        raise RuntimeError(f"failed to resolve feature id for {marker}: {resp.text[:500]}")
    fid = ids[0]
    print(f"Resolved feature id: {fid}")

    # Step 1: update reads the inserted row and writes the final value.
    resp = client.update_feature(fid, updated_marker)
    log_response("Step 1 update feature", resp, capture=True)
    if resp.status_code >= 400 or not wfs_success(resp.text):
        raise RuntimeError(f"update failed: {resp.status_code}\n{resp.text[:500]}")

    # Step 2: root reads the updated value.
    resp = client.get_feature_by_name(updated_marker)
    log_response("Step 2 read updated feature", resp, capture=True)
    ids = parse_feature_ids(resp.content)
    if fid not in ids:
        raise RuntimeError(f"root read did not return {fid}: ids={ids}, body={resp.text[:500]}")

    # Cleanup after root. This write is intentionally not part of gold and has a
    # later xid than the root snapshot, so it will not be selected as root's LW.
    resp = client.delete_feature(fid)
    log_response("Cleanup delete feature", resp, capture=False)


if __name__ == "__main__":
    main()
