# verify_project.ps1 v3
# Verificación de código. No verifica entorno, dependencias ni credenciales.
# Excluye env/, .venv/, venv/, site-packages/, node_modules/ de todos los recorridos.

$ErrorActionPreference = "Continue"
$PY = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $PY)) { $PY = ".\env\Scripts\python.exe" }
if (-not (Test-Path $PY)) { $PY = "python" }

$fails = 0
$EXCLUDE_DIRS = @('__pycache__', '.venv', 'venv', 'env', 'site-packages', 'node_modules', '.git')

function IsExcluded($path) {
    $parts = $path -split '\\'
    foreach ($p in $parts) {
        if ($EXCLUDE_DIRS -contains $p) { return $true }
    }
    return $false
}

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
Write-Host " VERIFICACION DEL PROYECTO (v3)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host "Python: $PY"

# ------------------------------------------------------------------
# 1. py_compile
# ------------------------------------------------------------------
Write-Host "`n>>> 1. py_compile" -ForegroundColor Cyan
$bad = @()
Get-ChildItem -Recurse -Filter *.py |
    Where-Object { -not (IsExcluded $_.FullName) } |
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
# 2. pytest
# ------------------------------------------------------------------
Check "2. pytest" "$PY -m pytest tests -q"

# ------------------------------------------------------------------
# 3. scan_unused (ya excluye env/.venv internamente)
# ------------------------------------------------------------------
Check "3. scan_unused" "$PY tools/scan_unused.py --entry run_reporter"

# ------------------------------------------------------------------
# 4. vulture
# ------------------------------------------------------------------
Write-Host "`n>>> 4. vulture" -ForegroundColor Cyan
$vulture = & vulture . --min-confidence 80 --exclude .venv,venv,env,__pycache__,logs,sitrad_data,node_modules,tools/legacy 2>&1
if ($vulture) {
    $vulture | ForEach-Object { Write-Host "    $_" }
    Write-Host "    [REVISAR] vulture encontro hallazgos" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] vulture limpio" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 5. pyflakes
# ------------------------------------------------------------------
Write-Host "`n>>> 5. pyflakes" -ForegroundColor Cyan
$pyflakes = & $PY -m pyflakes . 2>&1 | Where-Object {
    -not ($_ -match '\\(env|\.venv|venv|site-packages|__pycache__)\\')
}
if ($pyflakes) {
    $pyflakes | ForEach-Object { Write-Host "    $_" }
    Write-Host "    [REVISAR] pyflakes encontro hallazgos" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] pyflakes limpio" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 6. Import de todos los modulos
# ------------------------------------------------------------------
Write-Host "`n>>> 6. Import de todos los modulos" -ForegroundColor Cyan
$importScript = @'
import importlib, pkgutil, sys
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
    for b in bad: print("  ", b)
    sys.exit(1)
print("OK", len(roots), "paquetes")
'@
$tmp = Join-Path $env:TEMP "verify_imports.py"
$importScript | Set-Content -Encoding utf8 $tmp
& $PY $tmp
if ($LASTEXITCODE -ne 0) { $fails++ } else { Write-Host "    [OK] todos los modulos importan" -ForegroundColor Green }

# ------------------------------------------------------------------
# 7. Duplicados y __pycache__
# ------------------------------------------------------------------
Write-Host "`n>>> 7. Archivos duplicados" -ForegroundColor Cyan
$dups = Get-ChildItem -Recurse -Filter *.py |
    Where-Object { -not (IsExcluded $_.FullName) -and $_.Name -ne '__init__.py' } |
    Group-Object Name |
    Where-Object Count -gt 1
if ($dups) {
    $dups | ForEach-Object { Write-Host "    [DUPLICADO] $($_.Name) x$($_.Count)" -ForegroundColor Yellow }
} else {
    Write-Host "    [OK] 0 duplicados" -ForegroundColor Green
}

$pycache = Get-ChildItem -Recurse -Directory -Filter __pycache__ |
    Where-Object { -not ($_.FullName -match '\\(env|\.venv|venv)\\.*__pycache__') }
if ($pycache) {
    Write-Host "    [INFO] $($pycache.Count) __pycache__ (borrables con Remove-Item -Recurse)" -ForegroundColor Yellow
} else {
    Write-Host "    [OK] 0 __pycache__" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 8. Resultado
# ------------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Yellow
if ($fails -eq 0) {
    Write-Host " RESULTADO: OK" -ForegroundColor Green
} else {
    Write-Host " RESULTADO: $fails fallo(s)" -ForegroundColor Red
}
Write-Host "============================================================" -ForegroundColor Yellow
exit $fails