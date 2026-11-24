import requests
import urllib3
import json
urllib3.disable_warnings()

BASE = "https://localhost:8004"
AUTH = ("admin", "")
VERIFY = False

rutas_swagger = [
    "/swagger", "/swagger/index.html", "/swagger/v1/swagger.json",
    "/api-docs", "/v3/api-docs", "/openapi.json", "/api/v1/swagger.json",
    "/help", "/api/help", "/api"
]

print("Buscando Swagger en el puerto 8004...")
for ruta in rutas_swagger:
    try:
        r = requests.get(f"{BASE}{ruta}", auth=AUTH, verify=VERIFY, timeout=3)
        if r.status_code == 200:
            print(f"\n✅ ENCONTRADO: {BASE}{ruta}")
            if "json" in ruta:
                try:
                    data = r.json()
                    # Si es JSON, listamos las rutas disponibles
                    paths = data.get("paths", {})
                    print(f"Endpoints descubiertos: {len(paths)}")
                    for p, methods in paths.items():
                        print(f"  {p} -> {list(methods.keys())}")
                except:
                    print("Contenido:", r.text[:500])
            else:
                print("Es una página HTML. Ábrela en tu navegador:", f"{BASE}{ruta}")
            break
    except Exception:
        pass