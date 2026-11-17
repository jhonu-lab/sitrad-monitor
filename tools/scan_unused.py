"""
tools/scan_unused.py — módulos .py que nadie importa desde un punto de entrada.

    python tools/scan_unused.py --entry run_reporter [--root .]

Recorre los imports (también los hechos dentro de funciones) a partir del módulo de entrada y lista los .py del
proyecto que no se alcanzan. tools/ y tests/ se ignoran (son puntos de entrada propios). Sale con código 1 si hay huérfanos.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Directorios que nunca forman parte del código del proyecto:
# - puntos de entrada propios (tools/, tests/)
# - cachés y control de versiones
# - entornos virtuales (cualquier nombre habitual)
# - artefactos de build
# - datos generados por el bot (logs/, sitrad_data/)
IGNORE_DIRS = {
    "tools", "tests",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git",
    "venv", ".venv", "env", ".env",
    "site-packages", "dist-packages", "node_modules",
    "build", "dist", ".eggs",
    "logs", "sitrad_data",
}


def _is_venv_dir(d: Path) -> bool:
    """Un directorio es un venv (o un paquete instalado) si tiene pyvenv.cfg
    o si se llama site-packages / dist-packages. Se usa para excluirlo aunque
    no esté en IGNORE_DIRS (por ejemplo, si algún día se llama 'envs')."""
    if (d / "pyvenv.cfg").exists():
        return True
    return d.name in {"site-packages", "dist-packages"}


def module_name(root: Path, p: Path) -> str:
    rel = p.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path, mod: str, is_pkg: bool) -> set[str]:
    out: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return out
    pkg = mod if is_pkg else mod.rpartition(".")[0]
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                up = pkg.split(".") if pkg else []
                up = up[: len(up) - (n.level - 1)] if n.level > 1 else up
                base = ".".join(up + ([n.module] if n.module else []))
            out.add(base)
            out.update(f"{base}.{a.name}" if base else a.name for a in n.names)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", required=True)
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    root = Path(a.root).resolve()

    files = {}
    for p in root.rglob("*.py"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if IGNORE_DIRS & set(rel.parts):
            continue
        # Excluir si algún directorio padre es un venv / site-packages
        if any(_is_venv_dir(parent) for parent in p.parents if parent != root and parent.is_relative_to(root)):
            continue
        files[module_name(root, p)] = p

    if a.entry not in files:
        print(f"No encuentro el módulo de entrada '{a.entry}'.")
        return 2

    seen, todo = set(), [a.entry]
    while todo:
        m = todo.pop()
        if m in seen or m not in files:
            continue
        seen.add(m)
        p = files[m]
        for imp in imports_of(p, m, p.name == "__init__.py"):
            parts = imp.split(".")
            for i in range(1, len(parts) + 1):
                todo.append(".".join(parts[:i]))

    orphans = sorted(m for m in files if m not in seen and files[m].name != "__init__.py" and m != "")
    print(f"Módulos del proyecto: {len(files)} · alcanzados desde {a.entry}: {len(seen & set(files))}")
    if orphans:
        print("Huérfanos:")
        for m in orphans:
            print("  -", files[m].relative_to(root))
        return 1
    print("0 módulos huérfanos")
    return 0


if __name__ == "__main__":
    sys.exit(main())