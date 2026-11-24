import time
import requests
import urllib3
import subprocess
from datetime import datetime

urllib3.disable_warnings()

BASE = "https://localhost:8004/api/v1"
AUTH = ("admin", "admin12")
VERIFY = False

# Nombre del servicio en Windows (ajústalo si es diferente)
SERVICE_NAME = "SITRADService" 

def check_api():
    try:
        # 1. ¿El convertidor está activo?
        r_conv = requests.get(f"{BASE}/converters", auth=AUTH, verify=VERIFY, timeout=5)
        if r_conv.status_code == 200:
            convs = r_conv.json().get("results", [])
            if convs and convs[0].get("status") == "Inactive":
                return False, "Convertidor Inactivo"
                
        # 2. ¿El instrumento entrega valores?
        r_val = requests.get(f"{BASE}/instruments/1/values", auth=AUTH, verify=VERIFY, timeout=5)
        if r_val.status_code == 400:
            error = r_val.json().get("errors", ["Desconocido"])[0]
            return False, f"API bloqueada: {error}"
        elif r_val.status_code == 200:
            return True, "OK - Datos en vivo"
            
    except Exception as e:
        return False, f"Error de red: {e}"
    return False, "Respuesta inesperada"

def restart_sitrad():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Reiniciando servicio {SERVICE_NAME}...")
    try:
        subprocess.run(["net", "stop", SERVICE_NAME], capture_output=True)
        time.sleep(3)
        subprocess.run(["net", "start", SERVICE_NAME], capture_output=True)
        print("✅ Servicio reiniciado. Esperando 15s para que SITRAD arranque...")
        time.sleep(15)
    except Exception as e:
        print(f"❌ Error al reiniciar: {e}. (¿Ejecutaste como Administrador?)")

print("=== VIGILANTE DE SITRAD INICIADO ===")
print("Presiona Ctrl+C para salir.\n")

consecutive_failures = 0

try:
    while True:
        ok, msg = check_api()
        timestamp = datetime.now().strftime('%H:%M:%S')
        
        if ok:
            print(f"[{timestamp}] 🟢 {msg}")
            consecutive_failures = 0
        else:
            print(f"[{timestamp}] 🔴 {msg}")
            consecutive_failures += 1
            
            # Si falla 3 veces seguidas (ej. 1.5 minutos), forzamos el reinicio
            if consecutive_failures >= 3:
                restart_sitrad()
                consecutive_failures = 0
                
        time.sleep(30) # Espera 30 segundos entre chequeos

except KeyboardInterrupt:
    print("\nVigilante detenido.")