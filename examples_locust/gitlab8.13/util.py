import re
from loguru import logger
from bs4 import BeautifulSoup

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