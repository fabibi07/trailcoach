"""Construye el contexto `Estado del Atleta` consumido por el AI Coach.

El contexto está deliberadamente resumido: contiene fitness/fatiga/forma,
tendencias de carga/volumen, resúmenes de actividades recientes y evolución
de umbrales. NO contiene archivos FIT crudos, arrays de streams ni telemetría
por muestra.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.db.models import (
    Activity,
    ActivityLoad,
    Athlete,
    AthleteThreshold,
    DailyLoad,
    TrainingMetricDaily,
)


def _pace_min_km(duration_s: float | None, distance_m: float | None) -> float | None:
    if not duration_s or not distance_m or float(distance_m) <= 0:
        return None
    pace = (float(duration_s) / 60.0) / (float(distance_m) / 1000.0)
    return None if pace > 20 else pace


def _round_if(value: float | None, ndigits: int = 2) -> float | None:
    return None if value is None else round(float(value), ndigits)


def _fmt_pace(pace: float | None) -> str:
    if pace is None:
        return "-"
    m = int(pace)
    s = int(round((pace - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def _load_activities(
    db: Session, athlete_id: UUID, limit: int = 5
) -> list[dict[str, Any]]:
    rows = (
        db.query(Activity, ActivityLoad)
        .outerjoin(
            ActivityLoad,
            (ActivityLoad.activity_id == Activity.id) & ActivityLoad.is_primary.is_(True),
        )
        .filter(Activity.athlete_id == athlete_id)
        .order_by(Activity.start_time_utc.desc())
        .limit(limit)
        .all()
    )
    out = []
    for activity, load in rows:
        duration = float(activity.duration_moving_s or activity.duration_elapsed_s or 0) or None
        distance = float(activity.distance_m) if activity.distance_m else None
        pace = _pace_min_km(duration, distance)
        out.append(
            {
                "id": str(activity.id),
                "date": activity.start_time_utc.date().isoformat()
                if activity.start_time_utc
                else None,
                "sport": activity.sport,
                "sub_sport": activity.sub_sport,
                "distance_km": round(distance / 1000, 2) if distance else None,
                "duration_min": round(duration / 60, 1) if duration else None,
                "pace_min_km": pace,
                "pace_str": _fmt_pace(pace),
                "ascent_m": activity.ascent_m,
                "descent_m": activity.descent_m,
                "avg_hr": activity.avg_hr,
                "max_hr": activity.max_hr,
                "avg_power": activity.avg_power,
                "avg_cadence": activity.avg_cadence,
                "load_primary": round(float(load.value), 2) if load else None,
                "data_quality": activity.data_quality,
                "name": activity.name,
            }
        )
    return out


def _aggregate_daily_loads(
    db: Session, athlete_id: UUID, start: date, end: date
) -> dict[str, Any]:
    rows = (
        db.query(DailyLoad)
        .filter(
            DailyLoad.athlete_id == athlete_id,
            DailyLoad.date >= start,
            DailyLoad.date <= end,
        )
        .all()
    )
    load = 0.0
    duration_s = 0.0
    distance_m = 0.0
    ascent_m = 0.0
    n = 0
    has_degraded = False
    for r in rows:
        load += float(r.load_primary or 0)
        duration_s += float(r.duration_s or 0)
        distance_m += float(r.distance_m or 0)
        ascent_m += float(r.ascent_m or 0)
        n += int(r.n_activities or 0)
        if r.has_degraded:
            has_degraded = True
    return {
        "load": round(load, 2),
        "duration_h": round(duration_s / 3600, 2),
        "distance_km": round(distance_m / 1000, 2),
        "ascent_m": round(ascent_m, 2),
        "n_activities": n,
        "has_degraded": has_degraded,
    }


def _metric_at(
    db: Session, athlete_id: UUID, target: date
) -> TrainingMetricDaily | None:
    return (
        db.query(TrainingMetricDaily)
        .filter(
            TrainingMetricDaily.athlete_id == athlete_id,
            TrainingMetricDaily.date == target,
        )
        .first()
    )


def _latest_metric(
    db: Session, athlete_id: UUID, as_of: date
) -> TrainingMetricDaily | None:
    return (
        db.query(TrainingMetricDaily)
        .filter(
            TrainingMetricDaily.athlete_id == athlete_id,
            TrainingMetricDaily.date <= as_of,
        )
        .order_by(TrainingMetricDaily.date.desc())
        .first()
    )


def _threshold_evolution(
    db: Session, athlete_id: UUID, as_of: date
) -> dict[str, Any]:
    current_rows = (
        db.query(AthleteThreshold)
        .filter(
            AthleteThreshold.athlete_id == athlete_id,
            AthleteThreshold.valid_from <= as_of,
            (AthleteThreshold.valid_to.is_(None))
            | (AthleteThreshold.valid_to >= as_of),
        )
        .order_by(AthleteThreshold.kind, AthleteThreshold.valid_from.desc())
        .all()
    )
    current: dict[str, AthleteThreshold] = {}
    for row in current_rows:
        if row.kind not in current:
            current[row.kind] = row

    changes: list[dict[str, Any]] = []
    by_kind: dict[str, Any] = {}
    for kind, threshold in current.items():
        by_kind[kind] = {
            "value": float(threshold.value),
            "unit": threshold.unit,
            "valid_from": threshold.valid_from.isoformat(),
            "source": threshold.source,
        }
        previous = (
            db.query(AthleteThreshold)
            .filter(
                AthleteThreshold.athlete_id == athlete_id,
                AthleteThreshold.kind == kind,
                AthleteThreshold.valid_from < threshold.valid_from,
            )
            .order_by(AthleteThreshold.valid_from.desc())
            .first()
        )
        if previous:
            change = float(threshold.value) - float(previous.value)
            pct = (
                round(100.0 * change / float(previous.value), 2)
                if previous.value
                else None
            )
            changes.append(
                {
                    "kind": kind,
                    "previous": float(previous.value),
                    "current": float(threshold.value),
                    "unit": threshold.unit,
                    "change": round(change, 4),
                    "change_pct": pct,
                    "valid_from": threshold.valid_from.isoformat(),
                }
            )
    return {"current": by_kind, "recent_changes": changes}


def build_athlete_state(
    db: Session,
    athlete: Athlete,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Devuelve un contexto estructurado `Estado del Atleta` listo para el LLM."""
    today = as_of or date.today()
    week_start = today - timedelta(days=7)
    month_start = today - timedelta(days=28)
    prev_week_start = today - timedelta(days=14)
    prev_week_end = today - timedelta(days=7)

    current = _latest_metric(db, athlete.id, today)
    prev_week = _metric_at(db, athlete.id, today - timedelta(days=7))

    last_7d = _aggregate_daily_loads(db, athlete.id, week_start, today)
    prev_7d = _aggregate_daily_loads(db, athlete.id, prev_week_start, prev_week_end)
    last_28d = _aggregate_daily_loads(db, athlete.id, month_start, today)

    def _trend_key(current_val: float | None, prev_val: float | None) -> str:
        if current_val is None or prev_val is None or prev_val == 0:
            return "flat"
        current = float(current_val)
        previous = float(prev_val)
        diff = current - previous
        if diff > previous * 0.05:
            return "up"
        if diff < -previous * 0.05:
            return "down"
        return "flat"

    def _pct_change(current_val: float | None, prev_val: float | None) -> float | None:
        if current_val is None or prev_val is None or prev_val == 0:
            return None
        return round(100.0 * (float(current_val) - float(prev_val)) / float(prev_val), 1)

    trends = {
        "ctl": {
            "value": _round_if(current.ctl) if current else None,
            "value_7d_ago": _round_if(prev_week.ctl) if prev_week else None,
            "change_pct": _pct_change(
                current.ctl if current else None,
                prev_week.ctl if prev_week else None,
            ),
            "direction": _trend_key(
                current.ctl if current else None,
                prev_week.ctl if prev_week else None,
            ),
        },
        "load_7d": {
            "value": last_7d["load"],
            "previous": prev_7d["load"],
            "change_pct": _pct_change(last_7d["load"], prev_7d["load"]),
            "direction": _trend_key(last_7d["load"], prev_7d["load"]),
        },
        "volume_7d_km": {
            "value": last_7d["distance_km"],
            "previous": prev_7d["distance_km"],
            "change_pct": _pct_change(last_7d["distance_km"], prev_7d["distance_km"]),
            "direction": _trend_key(last_7d["distance_km"], prev_7d["distance_km"]),
        },
    }

    age = None
    if athlete.birth_date:
        age = today.year - athlete.birth_date.year
        if (today.month, today.day) < (
            athlete.birth_date.month,
            athlete.birth_date.day,
        ):
            age -= 1

    thresholds = _threshold_evolution(db, athlete.id, today)

    recent = _load_activities(db, athlete.id, limit=5)

    state = {
        "as_of": today.isoformat(),
        "athlete": {
            "id": str(athlete.id),
            "display_name": athlete.display_name,
            "sex": athlete.sex,
            "age": age,
            "units": athlete.units,
        },
        "fitness": {
            "ctl": _round_if(current.ctl) if current else None,
            "label": "forma",
        },
        "fatigue": {
            "atl": _round_if(current.atl) if current else None,
            "label": "fatiga",
        },
        "form": {
            "tsb": _round_if(current.tsb) if current else None,
            "label": _tsb_label(current.tsb) if current else "desconocido",
        },
        "ramp_rate_7d": _round_if(current.ramp_rate_7d) if current else None,
        "monotony_7d": _round_if(current.monotony_7d) if current else None,
        "strain_7d": _round_if(current.strain_7d) if current else None,
        "last_7d": last_7d,
        "last_28d": last_28d,
        "previous_7d": prev_7d,
        "trends": trends,
        "thresholds_active": thresholds["current"],
        "threshold_evolution": thresholds["recent_changes"],
        "recent_activities": recent,
    }

    state["summary"] = _summary_text(state)
    return state


def _tsb_label(tsb: float | None) -> str:
    if tsb is None:
        return "desconocido"
    if tsb > 25:
        return "muy alta"
    if tsb > 10:
        return "alta"
    if tsb < -30:
        return "muy baja"
    if tsb < -10:
        return "baja"
    return "neutral"


def _summary_text(state: dict[str, Any]) -> str:
    parts: list[str] = []
    a = state["athlete"]
    name = a.get("display_name") or "Atleta"
    fitness = state["fitness"]["ctl"]
    fatigue = state["fatigue"]["atl"]
    form = state["form"]["tsb"]
    form_label = state["form"]["label"]
    parts.append(
        f"Forma de {name}: {form_label} (CTL {fitness}, ATL {fatigue}, TSB {form})."
    )

    last_7 = state["last_7d"]
    if last_7["n_activities"]:
        parts.append(
            f"Últimos 7 días: {last_7['n_activities']} actividades, "
            f"{last_7['distance_km']} km, {last_7['ascent_m']} m D+, "
            f"carga {last_7['load']}."
        )
    else:
        parts.append("Sin actividades en los últimos 7 días.")

    trend = state["trends"]["ctl"]
    if trend["direction"] == "up":
        parts.append(f"El CTL está subiendo ({trend['change_pct']}% vs la semana pasada).")
    elif trend["direction"] == "down":
        parts.append(f"El CTL está bajando ({trend['change_pct']}% vs la semana pasada).")
    else:
        parts.append("El CTL es estable.")

    recent = state["recent_activities"]
    if recent:
        last = recent[0]
        parts.append(
            f"Actividad más reciente: {last['date']} {last['sport']} "
            f"{last['distance_km']} km en {last['duration_min']} min "
            f"({last['pace_str']} min/km), {last['ascent_m']} m D+."
        )

    return " ".join(parts)
