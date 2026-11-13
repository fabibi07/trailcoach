"""Recuperación diaria: series de bienestar y lectura de alertas.

Usa `WellnessDaily` y `WellnessSourceMetric` con `SourcePriority`, sin
depender de una marca concreta.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean, median
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.db.models import WellnessDaily, WellnessSourceMetric
from trailcoach.sources.priority import resolve_source_for_metric
from trailcoach.training.insights import Insight

EXTRA_METRICS = ("hrv_baseline_low_ms", "hrv_baseline_high_ms", "stress_avg")
RHR_DELTA_ALERT = 5.0
SLEEP_MIN_H = 6.0
SLEEP_SCORE_LOW = 60


def wellness_series(db: Session, athlete_id: UUID, days: int, today: date) -> list[dict[str, Any]]:
    start = today - timedelta(days=days)
    rows = (
        db.query(WellnessDaily)
        .filter(
            WellnessDaily.athlete_id == athlete_id,
            WellnessDaily.date >= start,
            WellnessDaily.date <= today,
        )
        .all()
    )
    source = resolve_source_for_metric(db, athlete_id, "wellness", sorted({r.source for r in rows}))
    extras: dict[date, dict[str, float]] = {}
    for m in (
        db.query(WellnessSourceMetric)
        .filter(
            WellnessSourceMetric.athlete_id == athlete_id,
            WellnessSourceMetric.source == source,
            WellnessSourceMetric.metric.in_(EXTRA_METRICS),
            WellnessSourceMetric.date >= start,
            WellnessSourceMetric.date <= today,
        )
        .all()
    ):
        extras.setdefault(m.date, {})[m.metric] = float(m.value)

    def f(v: Any) -> float | None:
        return float(v) if v is not None else None

    out = []
    for r in sorted((r for r in rows if r.source == source), key=lambda r: r.date):
        e = extras.get(r.date, {})
        out.append(
            {
                "date": r.date.isoformat(),
                "resting_hr": f(r.resting_hr),
                "hrv_ms": f(r.hrv_ms),
                "hrv_status": r.hrv_status,
                "hrv_baseline_low_ms": e.get("hrv_baseline_low_ms"),
                "hrv_baseline_high_ms": e.get("hrv_baseline_high_ms"),
                "sleep_score": r.sleep_score,
                "sleep_h": round(float(r.sleep_duration_s) / 3600, 2)
                if r.sleep_duration_s is not None
                else None,
                "body_battery_max": r.body_battery_max,
                "body_battery_min": r.body_battery_min,
                "stress_avg": e.get("stress_avg"),
            }
        )
    return out


def recovery_insights(series: list[dict[str, Any]], today: date) -> list[Insight]:
    """Alertas de recuperación a partir de los últimos días de bienestar."""
    if not series:
        return []
    last = series[-1]
    if (today - date.fromisoformat(last["date"])).days > 2:
        return []
    out: list[Insight] = []

    hrv, low = last.get("hrv_ms"), last.get("hrv_baseline_low_ms")
    high = last.get("hrv_baseline_high_ms")
    if hrv is not None and low is not None:
        if hrv < low:
            out.append(
                Insight(
                    "hrv",
                    "warn",
                    "HRV bajo tu rango normal",
                    f"Tu HRV de anoche ({hrv:.0f} ms) está bajo tu rango habitual "
                    f"({low:.0f}-{high or low:.0f} ms). Tu cuerpo no se ha recuperado: "
                    "hoy mejor un trote suave o descanso.",
                )
            )
        else:
            out.append(
                Insight(
                    "hrv",
                    "ok",
                    "HRV en tu rango normal",
                    f"Tu HRV de anoche ({hrv:.0f} ms) está dentro de tu rango habitual.",
                )
            )

    rhr_hist = [d["resting_hr"] for d in series[-31:-1] if d.get("resting_hr") is not None]
    rhr = last.get("resting_hr")
    if rhr is not None and len(rhr_hist) >= 7:
        base = median(rhr_hist)
        if rhr - base >= RHR_DELTA_ALERT:
            out.append(
                Insight(
                    "resting_hr",
                    "warn",
                    "FC en reposo alta",
                    f"Tu FC en reposo hoy ({rhr:.0f}) está {rhr - base:.0f} ppm sobre tu "
                    f"mediana del mes ({base:.0f}). Puede ser fatiga, estrés o el inicio de "
                    "una enfermedad.",
                )
            )

    sleeps = [d["sleep_h"] for d in series[-3:] if d.get("sleep_h") is not None]
    if len(sleeps) >= 2 and mean(sleeps) < SLEEP_MIN_H:
        out.append(
            Insight(
                "sleep",
                "warn",
                "Poco sueño",
                f"Promedio de {mean(sleeps):.1f} h de sueño en las últimas noches. "
                "Con menos de 6 h la recuperación cae: evita sesiones duras.",
            )
        )
    elif last.get("sleep_score") is not None and last["sleep_score"] < SLEEP_SCORE_LOW:
        out.append(
            Insight(
                "sleep",
                "info",
                "Sueño de baja calidad",
                f"Puntaje de sueño de anoche: {last['sleep_score']}.",
            )
        )
    return out
