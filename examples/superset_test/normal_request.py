import requests
import time
import re

url = 'http://localhost:8088'



def normal_request():
    session = requests.Session()
    login_api = url + '/login/'
    start_time = time.time()
    response = session.get(login_api)
    end_time = time.time()
    print("GET Login Page Time(ms): ", (end_time - start_time)*1000)
    csrf_token = re.findall(r'input id="csrf_token" name="csrf_token" type="hidden" value="(.*?)"', response.text)[0]
    
    login_data = {
        'csrf_token': csrf_token,
        'username': 'admin', 
        'password': 'vulhub'
    }
    start_time = time.time()
    response = session.post(login_api, data=login_data)
    end_time = time.time()
    print("POST Login Time(ms): ", (end_time - start_time)*1000)
    print(response.status_code)

    dashboard_api = url + '/api/v1/dashboard/'
    start_time = time.time()
    response = session.get(dashboard_api)
    end_time = time.time()
    print("GET Dashboard API Time(ms): ", (end_time - start_time)*1000)
    #print(response.status_code, response.headers['X-Request-Id'])
    session.close()

if __name__ == '__main__':
    total_time = 0
    for i in range(1):
        start_time = time.time()
        normal_request()
        end_time = time.time()
        total_time += end_time - start_time
    print("Total time(ms): ", total_time*1000)
    pass

"""
GET Login Page Time(ms):  43.321847915649414
POST Login Time(ms):  366.45030975341797
200
GET Dashboard API Time(ms):  62.011003494262695
Total time(ms):  473.17051887512207
"""

"""
docker logs -f superset_test-web-1

SQL rewrite 0.15ms/item 
Row Process 0.23ms/item

Overall overhead is about 8%
"""