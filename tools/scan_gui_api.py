import requests
import urllib3
urllib3.disable_warnings()

BASE = "http://localhost:8002"
AUTH = ("admin", "admin12")

endpoints = [
    "/api/instruments", "/api/devices", "/api/values", 
    "/api/v1/instruments/1/values", "/api/data", "/api/status",
    "/instruments", "/values", "/sitrad/api/instruments",
    "/api/v1/converters"
]

print("Escaneando puerto 8002 (GUI)...")
for ep in endpoints:
    try:
        r = requests.get(f"{BASE}{ep}", auth=AUTH, timeout=2)
        if r.status_code != 404:
            print(f"[{r.status_code}] {ep} -> {r.text[:100]}")
    except Exception as e:
        pass