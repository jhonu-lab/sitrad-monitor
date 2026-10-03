@echo off
REM ============================================================================
REM Arranca el bot daemon del Monitor Sitrad PRO.
REM Doble clic para ejecutar. Cierra la ventana para detener.
REM ============================================================================

cd /d "%~dp0"

REM Activar el entorno virtual
call .venv\Scripts\activate.bat

REM Codificación UTF-8 (para que los emojis se vean bien)
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

REM Crear carpeta de logs si no existe
if not exist "logs" mkdir logs

REM Arrancar el daemon
echo.
echo ============================================================
echo   MONITOR SITRAD PRO - BOT TELEGRAM
echo ============================================================
echo.
echo   Comandos disponibles desde Telegram:
echo     /demo    - Inicia una prueba simulada
echo     /status  - Ver estado actual
echo     /stop    - Cancelar demo en curso
echo     /ping    - Verificar que el bot responde
echo.
echo   Para DETENER: cierra esta ventana o presiona Ctrl+C
echo ============================================================
echo.

python run_bot_daemon.py

REM Si el script termina (crash o Ctrl+C), pausar para ver el error
echo.
echo ============================================================
echo   El bot daemon se detuvo.
echo ============================================================
pause