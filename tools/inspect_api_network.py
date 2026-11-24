"""Explorador profundo de la API de SITRAD. No modifica nada, solo lee y prueba."""
import requests
import json
import urllib3
import sys
urllib3.disable_warnings()

# Puertos comunes de SITRAD
PUERTS = [8002, 8004, 8000, 8080, 443, 80]
USER = "admin"
PWD = "admin12"

def test_url(base):
    print(f"\n--- Probando {base} ---")
    try:
        # 1. ¿Está vivo?
        r = requests.get(f"{base}/api/v1/instruments", auth=(USER, PWD), verify=False, timeout=3)
        print(f"GET /api/v1/instruments -> HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Instrumentos: {len(data.get('results', []))}")
            for ins in data.get('results', []):
                print(f"    · ID={ins.get('id')} Name={ins.get('name')} Status={ins.get('status')} ConverterID={ins.get('converterId')}")
            return True
    except Exception as e:
        print(f"  Fallo: {e}")
    
    # 2. Probar Swagger
    for path in ["/swagger", "/swagger/index.html", "/api-docs", "/openapi.json", "/api/v1/swagger.json"]:
        try:
            r = requests.get(f"{base}{path}", auth=(USER, PWD), verify=False, timeout=2)
            if r.status_code == 200:
                print(f"  Swagger encontrado en: {base}{path}")
                return True
        except:
            pass
    return False

def main():
    print("=== EXPLORACIÓN DE RED SITRAD ===")
    for port in PUERTS:
        for proto in ["https", "http"]:
            base = f"{proto}://localhost:{port}"
            if test_url(base):
                print(f"\n*** API ENCONTRADA EN: {base} ***")
                # Si la encontramos, intentamos ver los convertidores
                try:
                    r = requests.get(f"{base}/api/v1/converters", auth=(USER, PWD), verify=False, timeout=3)
                    print(f"\nConvertidores en {base}:")
                    print(json.dumps(r.json(), indent=2))
                except: pass

if __name__ == "__main__":
    main()