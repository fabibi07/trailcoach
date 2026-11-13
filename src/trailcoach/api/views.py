"""Endpoints de las vistas del dashboard: recuperación, intensidad, marcas, carrera y coach."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import numpy as np
import polars as pl
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import (
    Activity,
    ActivityLoad,
    ActivitySourceMetric,
    ActivityStreamSet,
    ActivityZoneTime,
    Athlete,
    RaceGoal,
)
from trailcoach.db.session import get_db
from trailcoach.training.best_efforts import DISTANCES_M, best_time_for_distance, vdot
from trailcoach.training.engine import activity_day_py
from trailcoach.training.intensity import decoupling_pct, weekly_distribution, zone_seconds
from trailcoach.training.race_plan import build_plan
from trailcoach.training.recovery import recovery_insights, wellness_series
from trailcoach.training.threshold_estimation import best_mean, guard_speed
from trailcoach.training.thresholds import get_thresholds

router = APIRouter()

INTENSITY_SPORTS = {"running", "trail_running", "hiking", "walking", "cycling"}
ROAD_SUBSPORTS = {None, "generic", "track", "road", "street"}

_stream_cache: dict[str, dict[str, np.ndarray]] = {}


def _athlete(db: Session) -> Athlete:
    athlete = db.query(Athlete).first()
    if athlete is None:
        raise HTTPException(404, "No athlete configured")
    return athlete


def _streams(ss: ActivityStreamSet, cols: tuple[str, ...]) -> dict[str, np.ndarray] | None:
    key = f"{ss.sha256}:{','.join(cols)}"
    if key in _stream_cache:
        return _stream_cache[key]
    path = settings.processed_root_path / ss.storage_path
    if not path.exists():
        return None
    schema = pl.read_parquet_schema(path)
    present = [c for c in cols if c in schema]
    if "t_s" not in present:
        return None
    df = pl.read_parquet(path, columns=present)
    out = {c: df[c].cast(pl.Float64).to_numpy() for c in present}
    _stream_cache[key] = out
    return out


def _activities_with_streams(db, athlete_id, start: date, sports: set[str]):
    q = (
        db.query(Activity, ActivityStreamSet)
        .join(ActivityStreamSet, ActivityStreamSet.activity_id == Activity.id)
        .filter(
            Activity.athlete_id == athlete_id,
            Activity.superseded_by.is_(None),
            Activity.sport.in_(sports),
            Activity.start_time_utc >= datetime.combine(start, datetime.min.time()),
        )
        .order_by(Activity.start_time_utc)
    )
    seen: set = set()
    for a, ss in q.all():
        if a.id in seen:
            continue
        seen.add(a.id)
        yield a, ss


@router.get("/v1/recovery")
def recovery(days: int = 90) -> dict[str, Any]:
    with get_db() as db:
        athlete = _athlete(db)
        today = date.today()
        series = wellness_series(db, athlete.id, days, today)
        return {
            "series": series,
            "insights": [i.__dict__ for i in recovery_insights(series, today)],
        }


@router.get("/v1/activities/{activity_id}/metrics")
def activity_metrics(activity_id: UUID) -> dict[str, Any]:
    """Métricas calculadas de una actividad: cargas, zonas, ritmo y desacople."""
    with get_db() as db:
        a = db.get(Activity, activity_id)
        if a is None:
            raise HTTPException(404, "Activity not found")
        loads = [
            {"method": lo.method, "value": round(float(lo.value), 1), "primary": lo.is_primary}
            for lo in db.query(ActivityLoad).filter(ActivityLoad.activity_id == activity_id)
        ]
        dist_km = float(a.distance_m) / 1000 if a.distance_m else None
        moving_s = float(a.duration_moving_s or a.duration_elapsed_s or 0) or None
        out: dict[str, Any] = {
            "loads": sorted(loads, key=lambda x: (not x["primary"], x["method"])),
            "pace_min_km": moving_s / 60 / dist_km if moving_s and dist_km else None,
            "zones_lthr": None,
            "lthr": None,
            "decoupling_pct": None,
            "device_hr_zones": [],
            "device": {},
        }
        for sm in db.query(ActivitySourceMetric).filter(
            ActivitySourceMetric.activity_id == activity_id,
            ActivitySourceMetric.metric.in_(DEVICE_METRICS),
        ):
            out["device"].setdefault(sm.metric, float(sm.value))
        zt = (
            db.query(ActivityZoneTime)
            .filter(
                ActivityZoneTime.activity_id == activity_id,
                ActivityZoneTime.zone_kind == "heart_rate",
            )
            .order_by(ActivityZoneTime.source, ActivityZoneTime.zone_index)
            .all()
        )
        if zt:
            src = zt[0].source
            out["device_hr_zones"] = [
                {
                    "zone": z.zone_index,
                    "minutes": round(float(z.seconds) / 60, 1),
                    "upper_bpm": float(z.upper_bound) if z.upper_bound is not None else None,
                }
                for z in zt
                if z.source == src
            ]
        ss = (
            db.query(ActivityStreamSet).filter(ActivityStreamSet.activity_id == activity_id).first()
        )
        st = _streams(ss, ("t_s", "hr_bpm", "speed_mps")) if ss else None
        if st and "hr_bpm" in st:
            lthr = get_thresholds(db, a.athlete_id, activity_day_py(a)).get("lthr_bpm")
            if lthr is not None:
                out["lthr"] = float(lthr.value)
                z = zone_seconds(st["t_s"], st["hr_bpm"], float(lthr.value))
                out["zones_lthr"] = {k: round(v / 60, 1) for k, v in z.items()}
            if "speed_mps" in st and a.sport == "running":
                out["decoupling_pct"] = decoupling_pct(st["t_s"], st["speed_mps"], st["hr_bpm"])
        return out


@router.get("/v1/weekly-intensity")
def weekly_intensity(weeks: int = 16) -> list[dict[str, Any]]:
    with get_db() as db:
        athlete = _athlete(db)
        start = date.today() - timedelta(weeks=weeks)
        per: list[tuple[date, dict[str, float]]] = []
        for a, ss in _activities_with_streams(db, athlete.id, start, INTENSITY_SPORTS):
            day = activity_day_py(a)
            lthr = get_thresholds(db, athlete.id, day).get("lthr_bpm")
            st = _streams(ss, ("t_s", "hr_bpm"))
            if lthr is None or st is None or "hr_bpm" not in st:
                continue
            per.append((day, zone_seconds(st["t_s"], st["hr_bpm"], float(lthr.value))))
        return weekly_distribution(per)


VDOT_KEYS = ("5k", "10k", "21k")
HIGHER_IS_BETTER = {"20min_mps", "vdot"}
DEVICE_METRICS = (
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "rpe",
    "feel_pct",
    "calories_kcal",
    "training_load_native",
)


@router.get("/v1/best-efforts")
def best_efforts(days: int = 730) -> dict[str, Any]:
    """Mejores tiempos por distancia y mejor ritmo de 20 min (carrera en ruta/pista)."""
    with get_db() as db:
        athlete = _athlete(db)
        start = date.today() - timedelta(days=days)
        efforts: list[dict[str, Any]] = []
        for a, ss in _activities_with_streams(db, athlete.id, start, {"running"}):
            if a.sub_sport not in ROAD_SUBSPORTS:
                continue
            st = _streams(ss, ("t_s", "distance_m", "speed_mps"))
            if st is None:
                continue
            row: dict[str, Any] = {
                "activity_id": str(a.id),
                "date": activity_day_py(a).isoformat(),
                "sub_sport": a.sub_sport,
            }
            if "distance_m" in st:
                for k, d in DISTANCES_M.items():
                    row[k] = best_time_for_distance(st["t_s"], st["distance_m"], d)
                vals = [vdot(DISTANCES_M[k], row[k]) for k in VDOT_KEYS if row.get(k) is not None]
                row["vdot"] = max([v for v in vals if v is not None], default=None)
            if "speed_mps" in st and a.sport == "running":
                ok = guard_speed(st["t_s"], st["speed_mps"])
                row["20min_mps"] = best_mean(st["t_s"][ok], st["speed_mps"][ok], 1200)
            efforts.append(row)

        keys = [*DISTANCES_M, "20min_mps", "vdot"]
        cutoff = (date.today() - timedelta(days=90)).isoformat()

        def best(rows: list[dict[str, Any]], k: str) -> dict[str, Any] | None:
            vals = [r for r in rows if r.get(k)]
            if not vals:
                return None
            pick = max if k in HIGHER_IS_BETTER else min
            r = pick(vals, key=lambda x: x[k])
            return {"value": r[k], "date": r["date"], "activity_id": r["activity_id"]}

        monthly: dict[str, dict[str, Any]] = {}
        for r in efforts:
            m = r["date"][:7]
            cur = monthly.setdefault(m, {"month": m})
            for k in keys:
                v = r.get(k)
                if v is None:
                    continue
                better = cur.get(k) is None or (v > cur[k] if k in HIGHER_IS_BETTER else v < cur[k])
                if better:
                    cur[k] = v
        return {
            "all_time": {k: best(efforts, k) for k in keys},
            "last_90d": {k: best([r for r in efforts if r["date"] >= cutoff], k) for k in keys},
            "monthly": [monthly[m] for m in sorted(monthly)],
        }


class RaceGoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    race_date: date
    distance_km: float = Field(gt=0, le=400)
    ascent_m: float | None = Field(default=None, ge=0, le=30000)
    target_time_h: float | None = Field(default=None, gt=0, le=100)


def _race_payload(db, athlete: Athlete, goal: RaceGoal) -> dict[str, Any]:
    from trailcoach.api.main import athlete_state_data

    today = date.today()
    state = athlete_state_data(db, athlete)
    start = today - timedelta(days=28)
    acts = (
        db.query(Activity)
        .filter(
            Activity.athlete_id == athlete.id,
            Activity.superseded_by.is_(None),
            Activity.sport.in_({"running", "trail_running", "hiking"}),
            Activity.start_time_utc >= datetime.combine(start, datetime.min.time()),
        )
        .all()
    )
    longest = max(acts, key=lambda a: float(a.duration_moving_s or 0), default=None)
    last28 = state["last_28d"]
    plan = build_plan(
        today=today,
        race_date=goal.race_date,
        distance_km=float(goal.distance_km),
        ascent_m=float(goal.ascent_m) if goal.ascent_m is not None else None,
        target_time_h=float(goal.target_time_h) if goal.target_time_h is not None else None,
        ctl=float(state["fitness"]["ctl"]) if state["fitness"] else None,
        tsb=float(state["form"]["tsb"]) if state["form"] else None,
        weekly_avg_km=last28["distance_km"] / 4,
        weekly_avg_ascent_m=last28["ascent_m"] / 4,
        longest_h_28d=float(longest.duration_moving_s or 0) / 3600 if longest else 0.0,
        longest_km_28d=float(longest.distance_m or 0) / 1000 if longest else 0.0,
    )
    return {
        "goal": {
            "name": goal.name,
            "race_date": goal.race_date.isoformat(),
            "distance_km": float(goal.distance_km),
            "ascent_m": float(goal.ascent_m) if goal.ascent_m is not None else None,
            "target_time_h": float(goal.target_time_h) if goal.target_time_h is not None else None,
        },
        "plan": plan,
    }


def _next_goal(db: Session, athlete: Athlete) -> RaceGoal | None:
    return (
        db.query(RaceGoal)
        .filter(RaceGoal.athlete_id == athlete.id, RaceGoal.race_date >= date.today())
        .order_by(RaceGoal.race_date)
        .first()
    )


def race_goal_data(db, athlete: Athlete) -> dict[str, Any] | None:
    goal = _next_goal(db, athlete)
    return _race_payload(db, athlete, goal) if goal else None


@router.get("/v1/race-goal")
def get_race_goal() -> dict[str, Any]:
    with get_db() as db:
        return {"race": race_goal_data(db, _athlete(db))}


@router.put("/v1/race-goal")
def put_race_goal(body: RaceGoalIn) -> dict[str, Any]:
    with get_db() as db:
        athlete = _athlete(db)
        goal = _next_goal(db, athlete)
        if goal is None:
            goal = RaceGoal(athlete_id=athlete.id)
            db.add(goal)
        goal.name = body.name
        goal.race_date = body.race_date
        goal.distance_km = body.distance_km
        goal.ascent_m = body.ascent_m
        goal.target_time_h = body.target_time_h
        db.flush()
        return {"race": _race_payload(db, athlete, goal)}


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


@router.get("/v1/ask/usage")
def ask_usage() -> dict[str, Any]:
    """Consumo de tokens del Coach IA en el mes actual (registro local)."""
    from trailcoach.ai.coach import monthly_usage

    return monthly_usage()


@router.post("/v1/ask")
def ask_coach(body: AskIn) -> dict[str, Any]:
    from trailcoach.ai.coach import ask
    from trailcoach.api.main import athlete_state_data

    with get_db() as db:
        athlete = _athlete(db)
        context = athlete_state_data(db, athlete)
        context["race_goal"] = race_goal_data(db, athlete)
        context["intensity_last_4_weeks"] = weekly_intensity(weeks=4)
        rec = wellness_series(db, athlete.id, 14, date.today())
        context["recovery_last_14d"] = rec
    return ask(body.question, context)
