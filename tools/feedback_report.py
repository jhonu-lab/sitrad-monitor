"""
tools/feedback_report.py — ¿acierta el bot según lo que el ingeniero ha corrido por chat?

    python tools/feedback_report.py [--cases logs/cooling_cases.jsonl] [--feedback logs/feedback.jsonl]
                                    [--csv casos.csv]

Imprime el acierto y los fallos por tipo de causa. Con --csv exporta un caso por fila (características +
causa real + veredicto): es el insumo para calibrar umbrales o entrenar un árbol cuando haya suficientes casos.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict

sys.path.insert(0, ".")
from analysis.feedback_log import CaseBook, bot_label


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="logs/cooling_cases.jsonl")
    ap.add_argument("--feedback", default="logs/feedback.jsonl")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()
    book = CaseBook(a.cases, a.feedback)
    print(book.accuracy_text())
    by = defaultdict(lambda: [0, 0])
    for cid in book.labels:
        if cid in book.cases:
            k = book.cases[cid]["bot_kind"]
            by[k][1] += 1
            by[k][0] += book.verdict(cid) == "acerto"
    if by:
        print("\nPor tipo de hipótesis del bot (aciertos / etiquetados):")
        for k, (ok, n) in sorted(by.items(), key=lambda kv: -kv[1][1]):
            print(f"  {bot_label(k):55s} {ok}/{n}")
    if a.csv:
        rows, keys = [], set()
        for cid, c in book.cases.items():
            lab = book.labels.get(cid, {})
            row = {"id": cid, "tipo": c.get("type"), "inicio": c.get("start"), "bot_kind": c.get("bot_kind"),
                   "causa_real": lab.get("real_cause") or "", "veredicto": book.verdict(cid) if lab else "",
                   **{f"f_{k}": v for k, v in (c.get("features") or {}).items()}}
            rows.append(row)
            keys.update(row)
        head = ["id", "tipo", "inicio", "bot_kind", "causa_real", "veredicto"] + sorted(k for k in keys if k.startswith("f_"))
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=head)
            w.writeheader()
            w.writerows(rows)
        print(f"\nExportados {len(rows)} casos a {a.csv} ({sum(1 for r in rows if r['causa_real'])} con causa real).")


if __name__ == "__main__":
    main()
