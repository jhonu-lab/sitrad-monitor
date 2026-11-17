# sitrad_monitor.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Paquetes del proyecto + dependencias con imports dinámicos
hiddenimports = []
for pkg in (
    "agent", "analysis", "core", "notifiers", "simulation", "sources", "tools",
    "pandas", "openpyxl", "requests", "urllib3", "matplotlib",
):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# matplotlib necesita sus data files (fuentes, estilos, kpsewhich, ...)
datas = []
for pkg in ("matplotlib", "openpyxl"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    ["run_reporter.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "tests", "PyQt5", "PySide2"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sitrad-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)