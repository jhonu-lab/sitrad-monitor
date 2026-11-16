# verify_project.ps1
# Verificación completa post-limpieza. Ejecutar desde la raíz del proyecto.

$ErrorActionPreference = "Continue"
$PY = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $PY)) { $PY = "python" }

$fails = 0
function Check($name, $cmd) {
    Write-Host "`n>>> $name" -ForegroundColor Cyan
    $out = Invoke-Expression $cmd 2>&1
    $code = $LASTEXITCODE
    if ($out) { $out | ForEach-Object { Write-Host "    $_" } }
    if ($code -ne 0) {
        Write-Host "    [FALLA] $name (exit=$code)" -ForegroundColor Red
        $script:fails++
        return $false
    }
    Write-Host "    [OK] $name" -ForegroundColor Green
    return $true
}

Write-Host "============================================================" -ForegroundColor Yellow
Write-Host " VERIFICACION COMPLETA DEL PROYECTO" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host "Python: $PY"

# ------------------------------------------------------------------
# 1. COMPILACION DE TODO EL ARBOL
# ------------------------------------------------------------------
Write-Host "`n>>> 1. py_compile (todo el arbol)" -ForegroundColor Cyan
$bad = @()
Get-ChildItem -Recurse -Filter *.py |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.FullName -notmatch '\\.venv\\' } |
    ForEach-Object {
        & $PY -m py_compile $_.FullName 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $bad += $_.FullName }
    }
if ($bad.Count -eq 0) {
    Write-Host "    [OK] 0 errores de compilacion" -ForegroundColor Green
} else {
    $bad | ForEach-Object { Write-Host "    [FALLA] $_" -ForegroundColor Red }
    $fails++
}

# ------------------------------------------------------------------
# 2. TESTS
# ------------------------------------------------------------------
Check "2. pytest (30 tests)" "$PY -m pytest tests -q"

# ------------------------------------------------------------------
# 3. MODULOS HUERFANOS
# ------------------------------------------------------------------
Check "3. scan_unused (0 huerfanos)" "$PY tools/scan_unused.py --entry run_reporter"

# ------------------------------------------------------------------
# 4. VULTURE (codigo sin uso)
# ------------------------------------------------------------------
Write-Host "`n>>> 4. vulture (80% confidence)" -ForegroundColor Cyan
$vulture = & vulture . --min-confidence 80 --exclude .venv,__pycache__,logs,sitrad_data,tools/legacy 2>&1
if ($vulture) {
    $vulture | ForEach-Object { Write-Host "    $_" }
    Write-Host "    [REVISAR] vulture encontro hallazgos (no necesariamente muertos)" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] vulture limpio" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 5. PYFLAKES
# ------------------------------------------------------------------
Write-Host "`n>>> 5. pyflakes" -ForegroundColor Cyan
$pyflakes = & $PY -m pyflakes . 2>&1
if ($pyflakes) {
    $pyflakes | ForEach-Object { Write-Host "    $_" }
    Write-Host "    [REVISAR] pyflakes encontro hallazgos" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] pyflakes limpio" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 6. IMPORTS EN CALIENTE (nada roto en runtime)
# ------------------------------------------------------------------
Write-Host "`n>>> 6. Import de todos los modulos" -ForegroundColor Cyan
$importScript = @'
import importlib, pkgutil, sys
from pathlib import Path
sys.path.insert(0, ".")

roots = ["agent", "analysis", "core", "notifiers", "simulation", "sources"]
bad = []
for r in roots:
    for m in pkgutil.walk_packages([r], prefix=r + "."):
        try:
            importlib.import_module(m.name)
        except Exception as e:
            bad.append(f"{m.name}: {e!r}")
for extra in ("run_reporter",):
    try:
        importlib.import_module(extra)
    except Exception as e:
        bad.append(f"{extra}: {e!r}")
if bad:
    print("FALLA")
    for b in bad:
        print("  ", b)
    sys.exit(1)
print("OK", len(roots), "paquetes")
'@
$tmp = Join-Path $env:TEMP "verify_imports.py"
$importScript | Set-Content -Encoding utf8 $tmp
& $PY $tmp
if ($LASTEXITCODE -ne 0) { $fails++ } else { Write-Host "    [OK] todos los modulos importan" -ForegroundColor Green }

# ------------------------------------------------------------------
# 7. ARCHIVOS OBSOLETOS
# ------------------------------------------------------------------
Write-Host "`n>>> 7. Archivos obsoletos" -ForegroundColor Cyan
$dups = Get-ChildItem -Recurse -Filter *.py |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.FullName -notmatch '\\.venv\\' -and $_.Name -ne '__init__.py' } |
    Group-Object Name |
    Where-Object Count -gt 1
if ($dups) {
    $dups | ForEach-Object { Write-Host "    [DUPLICADO] $($_.Name) x$($_.Count)" -ForegroundColor Yellow }
} else {
    Write-Host "    [OK] 0 duplicados de .py" -ForegroundColor Green
}

$pycache = Get-ChildItem -Recurse -Directory -Filter __pycache__ |
    Where-Object { $_.FullName -notmatch '\\.venv\\' }
if ($pycache) {
    Write-Host "    [INFO] $($pycache.Count) __pycache__ (se pueden borrar)" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] 0 __pycache__" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 8. RESULTADO
# ------------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Yellow
if ($fails -eq 0) {
    Write-Host " RESULTADO: OK - El proyecto esta limpio y funcional" -ForegroundColor Green
} else {
    Write-Host " RESULTADO: $fails fallo(s) - Revisar los [FALLA] de arriba" -ForegroundColor Red
}
Write-Host "============================================================" -ForegroundColor Yellow
exit $fails