#!/usr/bin/env python3
"""
Script de prueba: envía un mensaje de test a Telegram usando el token y
chat_id configurados en config.json.

Uso:
    python test_telegram.py
"""
import json
import sys
import requests


CONFIG_FILE = "config.json"


def main():
    # 1. Cargar config.json
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"❌ No se encontró {CONFIG_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Error parseando {CONFIG_FILE}: {e}")
        sys.exit(1)

    notifiers = cfg.get("notifiers", {})
    token = notifiers.get("telegram_token", "").strip()
    chat_id = notifiers.get("telegram_chat_id", "").strip()

    # 2. Validaciones previas
    if not token:
        print("❌ telegram_token está vacío en config.json")
        sys.exit(1)
    if not chat_id:
        print("❌ telegram_chat_id está vacío en config.json")
        sys.exit(1)
    if ":" not in token:
        print(f"⚠️  El token no contiene ':' — parece incompleto:")
        print(f"    '{token}'")
        sys.exit(1)

    print(f"Token:   {token[:12]}...{token[-6:]}")
    print(f"Chat ID: {chat_id}")
    print("Enviando mensaje de prueba...")

    # 3. Enviar
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": "✅ Prueba desde test_telegram.py — el Monitor Sitrad "
                    "puede enviar alertas correctamente.",
        }, timeout=10)
        data = r.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error de red: {e}")
        sys.exit(2)

    # 4. Interpretar respuesta
    if data.get("ok"):
        print("✅ Mensaje enviado correctamente. Revisa tu Telegram.")
    else:
        print(f"❌ Telegram respondió con error:")
        print(f"   {json.dumps(data, indent=2, ensure_ascii=False)}")
        # Diagnóstico automático
        desc = data.get("description", "").lower()
        if "chat not found" in desc:
            print("\n💡 El chat_id no existe o el bot nunca recibió un")
            print("   mensaje de ese chat. Abre Telegram, busca tu bot y")
            print("   envíale /start. Luego vuelve a ejecutar este script.")
        elif "unauthorized" in desc:
            print("\n💡 El token es inválido. Vuelve a @BotFather y")
            print("   copia el token completo (con el ':' en el medio).")
        sys.exit(3)


if __name__ == "__main__":
    main()