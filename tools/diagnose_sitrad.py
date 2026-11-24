"""Diagnóstico directo contra SITRAD. No arranca el bot."""
import sys
import requests
from requests.auth import HTTPBasicAuth
import urllib3
urllib3.disable_warnings()

BASE = "https://localhost:8004/api/v1"
AUTH = HTTPBasicAuth("admin", "admin12")
VERIFY = False

def show(label, r):
    print(f"\n=== {label}: HTTP {r.status_code} ===")
    print(r.text[:2000])

# 1. Instrumentos
r = requests.get(f"{BASE}/instruments", auth=AUTH, verify=VERIFY, timeout=30)
show("GET /instruments", r)
if r.status_code != 200:
    sys.exit("SITRAD no responde a /instruments")

instruments = r.json().get("results", [])
print(f"\nEncontrados {len(instruments)} instrumentos.")
for ins in instruments:
    iid = ins.get("id")
    print(f"  · id={iid} name={ins.get('name')} converterId={ins.get('converterId')}")

# 2. Valores por instrumento
for ins in instruments:
    iid = ins.get("id")
    r2 = requests.get(f"{BASE}/instruments/{iid}/values", auth=AUTH, verify=VERIFY, timeout=30)
    show(f"GET /instruments/{iid}/values", r2)

# 3. También prueba el endpoint de convertidores
r3 = requests.get(f"{BASE}/converters", auth=AUTH, verify=VERIFY, timeout=30)
show("GET /converters", r3)