# Admidio Second-Order SQL Injection CVE-2026-32813


## Steps

1. Log in and get the Session Cookie.
2. Get the CSRF Token and Role UUID.
3. Send the POST request above to inject the payload.
4. Request the redirected `lists_show.php` link.
5. Measure response time >= 3 seconds to confirm the vulnerability.
