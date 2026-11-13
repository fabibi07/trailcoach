"""Distribución de intensidad por pulso (modelo de 3 zonas sobre la LTHR).

Suave: < 90 % LTHR. Medio: 90-100 % LTHR. Fuerte: > LTHR.
Se calcula desde los streams y no modifica los datos originales.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

LOW_MAX = 0.90
GAP_MAX_S = 10.0


def zone_seconds(t_s: np.ndarray, hr: np.ndarray, lthr: float) -> dict[str, float]:
    """Segundos en cada zona; cada muestra pesa el tiempo hasta la siguiente."""
    if len(t_s) < 2 or lthr <= 0:
        return {"low": 0.0, "mid": 0.0, "high": 0.0}
    dt = np.diff(t_s, append=t_s[-1])
    dt = np.clip(dt, 0, GAP_MAX_S)
    ok = ~np.isnan(hr) & (hr >= 30) & (hr <= 220)
    hr, dt = hr[ok], dt[ok]
    low = hr < LOW_MAX * lthr
    high = hr > lthr
    mid = ~low & ~high
    return {
        "low": float(dt[low].sum()),
        "mid": float(dt[mid].sum()),
        "high": float(dt[high].sum()),
    }


def weekly_distribution(per_activity: list[tuple[date, dict[str, float]]]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, float]] = {}
    for day, z in per_activity:
        y, w, _ = day.isocalendar()
        key = f"{y}-W{w:02d}"
        a = agg.setdefault(key, {"low": 0.0, "mid": 0.0, "high": 0.0})
        for k in a:
            a[k] += z[k]
    out = []
    for key, a in sorted(agg.items()):
        total = sum(a.values())
        out.append(
            {
                "week": key,
                **{f"{k}_h": round(v / 3600, 2) for k, v in a.items()},
                **{f"{k}_pct": round(100 * v / total, 1) if total else 0.0 for k, v in a.items()},
            }
        )
    return out
