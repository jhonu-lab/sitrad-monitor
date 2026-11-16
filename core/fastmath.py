"""Media y desviación muestral en coma flotante.

statistics.mean/stdev usan aritmética exacta con fracciones: correctas pero
~50 veces más lentas, y fit() las llama miles de veces. Misma semántica
(incluidos los errores en listas vacías) con math.fsum.
"""
from __future__ import annotations

import math
from statistics import StatisticsError


def mean(data) -> float:
    xs = data if isinstance(data, (list, tuple)) else list(data)
    if not xs:
        raise StatisticsError("mean requires at least one data point")
    return math.fsum(xs) / len(xs)


def stdev(data) -> float:
    xs = data if isinstance(data, (list, tuple)) else list(data)
    n = len(xs)
    if n < 2:
        raise StatisticsError("stdev requires at least two data points")
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))
