# tools/find_duplicates.py
"""Encuentra .py con mismo nombre o mismo contenido en el proyecto."""
from __future__ import annotations
import hashlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(".")
SKIP = {".venv", "venv", "__pycache__", ".git", "logs", "sitrad_data", "node_modules"}

by_name: dict[str, list[Path]] = defaultdict(list)
by_hash: dict[str, list[Path]] = defaultdict(list)

for p in ROOT.rglob("*.py"):
    if any(part in SKIP for part in p.parts):
        continue
    if p.name == "__init__.py":
        continue
    by_name[p.name].append(p)
    by_hash[hashlib.md5(p.read_bytes()).hexdigest()].append(p)

print("== Archivos con el MISMO NOMBRE ==")
for name, paths in sorted(by_name.items()):
    if len(paths) > 1:
        print(f"  {name}:")
        for p in paths:
            print(f"    {p}")

print("\n== Archivos con el MISMO CONTENIDO ==")
for h, paths in sorted(by_hash.items()):
    if len(paths) > 1:
        print(f"  {h[:8]}:")
        for p in paths:
            print(f"    {p}")