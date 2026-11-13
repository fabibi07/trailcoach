"""Plan orientativo hacia una carrera objetivo (heurísticas simples, no prescripción)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

LONG_RACE_KM = 30.0


def taper_days(distance_km: float) -> int:
    return 14 if distance_km >= LONG_RACE_KM else 10


def phase(days_left: int, taper: int) -> tuple[str, str]:
    if days_left < 0:
        return "done", "La carrera ya pasó. Define una nueva carrera objetivo."
    if days_left <= 7:
        return "race_week", "Semana de carrera: sesiones cortas, algo de ritmo y mucho descanso."
    if days_left <= taper:
        return "taper", "Taper: baja el volumen un 30-50 % y mantén algo de intensidad."
    if days_left <= taper + 28:
        return "peak", "Bloque específico: tiradas largas y D+ parecidos a la carrera."
    return "build", "Construcción: sube volumen y D+ de forma gradual (≤10 % por semana)."


def build_plan(
    *,
    today: date,
    race_date: date,
    distance_km: float,
    ascent_m: float | None,
    target_time_h: float | None,
    ctl: float | None,
    tsb: float | None,
    weekly_avg_km: float,
    weekly_avg_ascent_m: float,
    longest_h_28d: float,
    longest_km_28d: float,
) -> dict[str, Any]:
    days_left = (race_date - today).days
    taper = taper_days(distance_km)
    ph, advice = phase(days_left, taper)
    # km-esfuerzo = km + D+/100, a ~7 km-esfuerzo/h si no hay tiempo objetivo
    est_h = target_time_h or (distance_km + (ascent_m or 0) / 100.0) / 7.0
    long_target_h = round(min(est_h * 0.65, 5.0), 1)
    ascent_week_target = round((ascent_m or 0) * (1.0 if distance_km <= 50 else 0.7))
    km_week_target = min(distance_km * 1.2, 100.0)
    checks = [
        {
            "label": "Tirada larga (últimas 4 semanas)",
            "current": round(longest_h_28d, 1),
            "target": long_target_h,
            "unit": "h",
            "ok": longest_h_28d >= long_target_h * 0.9,
        },
        {
            "label": "D+ semanal promedio",
            "current": round(weekly_avg_ascent_m),
            "target": ascent_week_target,
            "unit": "m",
            "ok": weekly_avg_ascent_m >= ascent_week_target * 0.8,
        },
        {
            "label": "Km semanales promedio",
            "current": round(weekly_avg_km, 1),
            "target": round(km_week_target, 1),
            "unit": "km",
            "ok": weekly_avg_km >= km_week_target * 0.8,
        },
    ]
    return {
        "days_left": days_left,
        "weeks_left": round(days_left / 7, 1),
        "taper_start": (race_date - timedelta(days=taper)).isoformat(),
        "phase": ph,
        "advice": advice,
        "estimated_time_h": round(est_h, 1),
        "ctl": ctl,
        "tsb": tsb,
        "race_day_tsb_target": [5, 25],
        "longest_km_28d": round(longest_km_28d, 1),
        "checks": checks,
    }
