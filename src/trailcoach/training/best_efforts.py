"""Mejores marcas por distancia y por duración, desde los streams."""

from __future__ import annotations

import numpy as np

DISTANCES_M = {"1k": 1000.0, "5k": 5000.0, "10k": 10000.0, "21k": 21097.5}
SPEED_MAX_VALID = 7.0


def clean_distance(t_s: np.ndarray, dist_m: np.ndarray) -> np.ndarray:
    """Distancia acumulada sin saltos de GPS: incrementos > SPEED_MAX_VALID cuentan 0."""
    inc = np.diff(dist_m, prepend=dist_m[0])
    dt = np.diff(t_s, prepend=t_s[0])
    bad = (inc < 0) | (inc > SPEED_MAX_VALID * np.maximum(dt, 1.0))
    inc = np.where(bad, 0.0, inc)
    out: np.ndarray = dist_m[0] + np.cumsum(inc)
    return out


def best_time_for_distance(t_s: np.ndarray, dist_m: np.ndarray, target_m: float) -> float | None:
    """Menor tiempo (s) para cubrir `target_m` dentro de la actividad (dos punteros)."""
    ok = ~np.isnan(t_s) & ~np.isnan(dist_m)
    t = t_s[ok]
    if len(t) < 2:
        return None
    d = clean_distance(t, dist_m[ok])
    if len(d) < 2 or d[-1] - d[0] < target_m:
        return None
    best: float | None = None
    j = 0
    for i in range(len(d)):
        while j < len(d) and d[j] - d[i] < target_m:
            j += 1
        if j >= len(d):
            break
        dt = float(t[j] - t[i])
        if dt > 0 and target_m / dt <= SPEED_MAX_VALID and (best is None or dt < best):
            best = dt
    return best
