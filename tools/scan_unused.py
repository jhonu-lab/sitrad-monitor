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

IGNORE_DIRS = {"tools", "tests", "__pycache__", ".git", "venv", ".venv", "logs", "sitrad_data"}


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
        if IGNORE_DIRS & set(p.relative_to(root).parts):
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
            for i in range(1, len(parts) + 1):               # el paquete y todos sus padres también se cargan
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
