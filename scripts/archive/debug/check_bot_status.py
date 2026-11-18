import urllib.request
import json

try:
    r = urllib.request.urlopen("http://127.0.0.1:8091/api/status", timeout=5)
    d = json.loads(r.read())
    print(f"Status OK: is_running={d.get('is_running')} last_scan={d.get('last_scan')}")
except Exception as e:
    print(f"Status check failed: {e}")
