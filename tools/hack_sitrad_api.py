import requests
import urllib3
urllib3.disable_warnings()

BASE = "https://localhost:8004/api/v1"
AUTH = ("admin", "admin12")
VERIFY = False

def try_request(method, endpoint, description):
    url = f"{BASE}{endpoint}"
    print(f"Intentando {description}: {method} {endpoint} ... ", end="")
    try:
        r = requests.request(method, url, auth=AUTH, verify=VERIFY, timeout=5)
        if r.status_code in (200, 201, 202, 204):
            print(f"✅ ÉXITO (HTTP {r.status_code})")
            return True
        else:
            print(f"❌ Fallo (HTTP {r.status_code})")
            try:
                print(f"   Body: {r.text[:100]}")
            except: pass
    except Exception as e:
        print(f"❌ Error de red: {e}")
    return False

# 1. Intentar borrar el convertidor fantasma (ID 2)
# Esto podría limpiar la base de datos y forzar a recargar el ID 3
if try_request("DELETE", "/converters/2", "Borrar convertidor fantasma (ID 2)"):
    print("-> Convertidor borrado. Ejecuta diagnose_sitrad.py de nuevo.")

# 2. Intentar recargar la configuración del servidor
try_request("POST", "/servers/reload", "Recargar configuración del servidor")
try_request("POST", "/servers/restart", "Reiniciar servidor API")
try_request("POST", "/converters/reload", "Recargar convertidores")
try_request("POST", "/instruments/reload", "Recargar instrumentos")

# 3. Intentar activar el convertidor a la fuerza
try_request("PUT", "/converters/2/status", "Forzar activación del convertidor 2")
try_request("POST", "/converters/2/connect", "Conectar convertidor 2")