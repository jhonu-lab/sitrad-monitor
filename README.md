================================================================================
 PROYECTO: MONITOR SITRAD PRO — Servicio de Monitoreo en Tiempo Real
================================================================================

DESCRIPCIÓN
-----------
Servicio en segundo plano (Worker/Daemon) que consume la API REST local de
Sitrad PRO (Full Gauge Controls), detecta fallas en controladores e
instrumentos (estados "Err", valores fuera de setpoint/histéresis, offline)
y envía alertas automáticas a Telegram / Email.

Incluye un SIMULADOR que replica la API de Sitrad PRO para demostrar el
funcionamiento completo sin acceso al servidor real de la empresa.


REQUISITOS
----------
- Python 3.10 o superior
- pip
- (Producción) Sitrad PRO en localhost con API REST habilitada
- (Producción) Bot de Telegram con token y chat_id si se quiere notificar


INSTALACIÓN (una sola vez)
--------------------------
  pip install -r requirements.txt


--------------------------------------------------------------------------------
 MODO DEMOSTRACIÓN (sin Sitrad real)
--------------------------------------------------------------------------------
  1. Verificar en config.yaml:  mode: "demo"
  2. Ejecutar:
       python run_demo.py

Qué hace:
  - Levanta un simulador Flask en http://localhost:8001 que replica el JSON
    de Sitrad PRO.
  - Ejecuta 25 ciclos de polling (uno cada 2 s) con 5 escenarios:
        ticks 0-4   → todo normal
        ticks 5-9   → MT-512E reporta "Err" en Sensor Evaporador
        ticks 10-14 → PCT-3001 pasa a Offline
        ticks 15-19 → RCK-862 reporta presión muy fuera de rango
        ticks 20+   → recuperación total
  - Muestra un DASHBOARD en vivo en la terminal (rich).
  - Registra todo en logs/alerts.jsonl.


--------------------------------------------------------------------------------
 MODO PRODUCCIÓN (Sitrad real)
--------------------------------------------------------------------------------
  1. Copiar el proyecto SIN la carpeta simulator/.
  2. Editar config.yaml:
        mode: "production"
        telegram: true
        telegram_token: "123456:ABC..."
        telegram_chat_id: "-1001234567890"
  3. Ejecutar:
        python run_production.py

Instalación como servicio systemd (Linux):

  sudo nano /etc/systemd/system/sitrad-monitor.service

  [Unit]
  Description=Sitrad PRO Monitoring Worker
  After=network.target

  [Service]
  Type=simple
  User=iot
  WorkingDirectory=/opt/sitrad-monitor
  ExecStart=/usr/bin/python3 /opt/sitrad-monitor/run_production.py
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target

  sudo systemctl daemon-reload
  sudo systemctl enable sitrad-monitor
  sudo systemctl start sitrad-monitor
  sudo systemctl status sitrad-monitor


--------------------------------------------------------------------------------
 DIFERENCIA ENTRE DEMO Y PRODUCCIÓN
--------------------------------------------------------------------------------
El código del núcleo (core/) es EXACTAMENTE EL MISMO en ambos modos.
Lo único que cambia es:
  - La URL del host            → config.yaml (mode: demo | production)
  - Si se levanta el simulador → run_demo.py sí, run_production.py no
  - Qué notificadores activos  → config.yaml

Esto demuestra Inversión de Dependencias: el cliente HTTP no sabe (ni le
importa) si del otro lado está el simulador o el Sitrad real.


--------------------------------------------------------------------------------
 ESTRUCTURA DE ARCHIVOS
--------------------------------------------------------------------------------
sitrad-monitor/
├── README.txt
├── ARCHITECTURE.md
├── requirements.txt
├── config.yaml
├── run_demo.py
├── run_production.py
├── simulator/
│   ├── fake_sitrad_api.py
│   ├── scenarios.py
│   └── device_factory.py
├── core/
│   ├── models.py
│   ├── sitrad_client.py
│   ├── rule_engine.py
│   ├── deduplication.py
│   └── worker.py
├── notifiers/
│   ├── base.py
│   ├── console.py
│   ├── file_log.py
│   ├── telegram.py
│   └── callback.py
├── dashboard/
│   └── live_view.py
└── logs/
    └── alerts.jsonl


--------------------------------------------------------------------------------
 REGLAS DEL MOTOR DE DETECCIÓN
--------------------------------------------------------------------------------
R1. Sensor con valor "Err"                 → CRÍTICO
R2. Dispositivo Offline                    → CRÍTICO
R3. Valor fuera de setpoint ± histéresis   → ADVERTENCIA
R4. Desviación > 2× histéresis             → CRÍTICO
R5. Recuperación (fin de cualquier alerta) → NOTIFICACIÓN VERDE

Mecanismos anti-spam:
  - Histéresis temporal: N lecturas consecutivas antes de confirmar.
  - Cooldown: no reenvía la misma alerta dentro de la ventana.
  - Detección de recuperación: avisa cuando la condición desaparece.


--------------------------------------------------------------------------------
 FORMATO DE ALERTA
--------------------------------------------------------------------------------
  [SEVERIDAD] [EQUIPO] [SENSOR/CANAL]
  [VALOR DETECTADO] [TIMESTAMP] [ACCIÓN SUGERIDA]

Ejemplo:
  🔴 [CRÍTICO] | Cámara Congelados
     Sensor:  Temp Evaporador
     Valor:   Err
     Hora:    2026-01-15 10:23:47
     Acción:  Verificar conexión física del sensor (NTC/Pt-100).


--------------------------------------------------------------------------------
 PREGUNTAS FRECUENTES
--------------------------------------------------------------------------------
¿Por qué no usa IA/ML?
  El motor determinista es suficiente para detección con latencia mínima y
  auditable. La IA se reserva para una fase futura de diagnóstico contextual.

¿Qué pasa si Sitrad está caído?
  El cliente HTTP reintenta 3 veces con backoff exponencial. Si tras eso
  falla, registra el error y continúa. El worker nunca se detiene por fallo
  de red.

¿Puedo cambiar los umbrales?
  Sí, todos en config.yaml.

================================================================================
 FIN DEL README
================================================================================