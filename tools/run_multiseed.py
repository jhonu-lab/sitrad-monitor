"""
tools/run_multiseed.py — ¿el diagnóstico acierta de forma CONSISTENTE?

    python tools/run_multiseed.py [--seeds 20] [--first 1] [--days 4]
                                  [--config config.json] [--out logs/multiseed]

Una sola semilla da 74% o 90% según la suerte (con ~25 casos, dos errores
mueven el resultado 8 puntos). Aquí se corren N semillas y se reporta el
promedio, el mínimo, el máximo y cuántas semillas cumplen la meta, además de
los errores que MÁS se repiten, que son los que hay que arreglar.
"""
from __future__ import annotations

import argparse
import os
import statistics as st
import sys
from collections import Counter

sys.path.insert(0, ".")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import case_report
from run_validation import simulate


def main(argv=None) -> list[dict]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--days", type=float, default=4.0)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="logs/multiseed")
    ap.add_argument("--target", type=float, default=0.8)
    ap.add_argument("--warmup-normal-h", type=float, default=2.5)
    a = ap.parse_args(argv)

    res = []
    for seed in range(a.first, a.first + a.seeds):
        out = os.path.join(a.out, f"seed_{seed}")
        cases, truth, _ = simulate(config=a.config, out=out, days=a.days, seed=seed,
                                   warmup_normal_h=a.warmup_normal_h, quiet=True)
        m = case_report.main(["--cases", cases, "--truth", truth,
                              "--labels", os.path.join(out, "case_labels.jsonl"),
                              "--warmup-h", str(a.warmup_normal_h),
                              "--target", str(a.target), "--quiet"])
        m["seed"] = seed
        res.append(m)
        print(f"  seed {seed:>3}: acierto {m['acierto']:6.1%}  casos {m['casos']:>3}  "
              f"recall {m['recall']:5.0%}  dx/episodio {m['dx_episodios']:5.0%}  "
              f"falsas {m['falsas']}", flush=True)

    ok = [m for m in res if m["acierto"] >= a.target]
    acc = [m["acierto"] for m in res]
    tot_casos = sum(m["casos"] for m in res)
    tot_ok = sum(m["casos_ok"] for m in res)
    print("\n" + "=" * 66)
    print(f"RESUMEN SOBRE {len(res)} SEMILLAS ({a.days:g} días c/u)")
    print("=" * 66)
    print(f"Acierto por semilla:  media {st.mean(acc):.1%} · mediana {st.median(acc):.1%} · "
          f"mín {min(acc):.1%} · máx {max(acc):.1%}")
    print(f"Semillas que cumplen {a.target:.0%}: {len(ok)}/{len(res)}")
    print(f"Acierto agregado:     {tot_ok}/{tot_casos} = {tot_ok / max(tot_casos, 1):.1%}")
    print(f"Detección (recall):   media {st.mean(m['recall'] for m in res):.1%} · "
          f"mín {min(m['recall'] for m in res):.0%}")
    print(f"Diagnóstico correcto por episodio inyectado: "
          f"{st.mean(m['dx_episodios'] for m in res):.1%}")
    print(f"Falsas alarmas:       {sum(m['falsas'] for m in res)} en total · "
          f"{sum(m['silenciados'] for m in res)} silenciadas · "
          f"{sum(m['consecuencias'] for m in res)} por consecuencia")

    inc = Counter(x for m in res for x in m["incorrectos"])
    if inc:
        print("\nDiagnósticos incorrectos más repetidos (real -> dijo):")
        for (r, d), n in inc.most_common(8):
            print(f"  {n:>3} × {r} -> {d}")
    per = Counter(n for m in res for n, _ in m["perdidos"])
    if per:
        print("\nFallas sin incidente más repetidas:")
        for n, c in per.most_common(8):
            print(f"  {c:>3} × {n}")
    fa = Counter(m_ for m in res for m_ in m["falsas_dx"])
    if fa:
        print("\nFalsas alarmas por diagnóstico emitido:")
        for n, c in fa.most_common(6):
            print(f"  {c:>3} × {n}")
    return res


if __name__ == "__main__":
    main()
