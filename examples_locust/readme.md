# Locust Background Traffic Test

```bash
cd tests_locust/{app_name}
locust -f locustfile.py

http://10.0.0.252:8089/
```

```bash
locust -f ./locustfile.py --headless -u 5 -r 5 --run-time 80s
```