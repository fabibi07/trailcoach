"""FastAPI application."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import polars as pl
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func

from trailcoach.ai.coach import ask as ask_coach
from trailcoach.ai.context import build_athlete_state
from trailcoach.db.models import (
    Activity,
    ActivityInterval,
    ActivityStreamChannel,
    ActivityStreamSet,
    Athlete,
    AthleteThreshold,
    DailyLoad,
    SourceActivity,
    TrainingMetricDaily,
)
from trailcoach.db.session import SessionLocal, get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="TrailCoach", version="0.1.0", lifespan=lifespan)

FRONTEND_DIR = Path(__file__).parent.parent.parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    if FRONTEND_DIR.exists():
        return FileResponse(FRONTEND_DIR / "index.html")
    return {"message": "TrailCoach API is running"}


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/athlete")
def get_athlete():
    db = next(_get_db())
    athlete = db.query(Athlete).first()
    if not athlete:
        raise HTTPException(404, "No athlete configured")
    return {
        "id": str(athlete.id),
        "display_name": athlete.display_name,
        "sex": athlete.sex,
        "tz_default": athlete.tz_default,
        "units": athlete.units,
    }


@app.get("/v1/activities")
def list_activities(from_date: date | None = None, to_date: date | None = None, limit: int = 50):
    db = next(_get_db())
    q = db.query(Activity)
    if from_date:
        q = q.filter(func.date(Activity.start_time_utc) >= from_date)
    if to_date:
        q = q.filter(func.date(Activity.start_time_utc) <= to_date)
    q = q.order_by(Activity.start_time_utc.desc()).limit(limit)
    return [
        {
            "id": str(a.id),
            "start_time_utc": a.start_time_utc.isoformat() if a.start_time_utc else None,
            "sport": a.sport,
            "distance_km": round(a.distance_m / 1000, 3) if a.distance_m else None,
            "duration_min": round(a.duration_elapsed_s / 60, 2) if a.duration_elapsed_s else None,
            "ascent_m": a.ascent_m,
            "data_quality": a.data_quality,
            "name": a.name,
        }
        for a in q.all()
    ]


@app.get("/v1/activities/{activity_id}")
def get_activity(activity_id: UUID):
    db = next(_get_db())
    a = db.query(Activity).filter(Activity.id == activity_id).first()
    if not a:
        raise HTTPException(404, "Activity not found")
    sources = (
        db.query(SourceActivity)
        .filter(SourceActivity.canonical_activity_id == activity_id)
        .all()
    )
    intervals = (
        db.query(ActivityInterval)
        .filter(ActivityInterval.activity_id == activity_id)
        .order_by(ActivityInterval.seq)
        .all()
    )
    stream_set = (
        db.query(ActivityStreamSet)
        .filter(ActivityStreamSet.activity_id == activity_id)
        .first()
    )
    channels = []
    if stream_set:
        channels = (
            db.query(ActivityStreamChannel)
            .filter(ActivityStreamChannel.stream_set_id == stream_set.id)
            .all()
        )
    return {
        "id": str(a.id),
        "start_time_utc": a.start_time_utc.isoformat() if a.start_time_utc else None,
        "sport": a.sport,
        "distance_km": round(a.distance_m / 1000, 3) if a.distance_m else None,
        "duration_min": round(a.duration_elapsed_s / 60, 2) if a.duration_elapsed_s else None,
        "ascent_m": a.ascent_m,
        "descent_m": a.descent_m,
        "avg_hr": a.avg_hr,
        "max_hr": a.max_hr,
        "avg_power": a.avg_power,
        "avg_cadence": a.avg_cadence,
        "data_quality": a.data_quality,
        "quality_flags": a.quality_flags,
        "sources": [
            {"source": s.source, "source_id": s.source_activity_id, "device": s.device_name}
            for s in sources
        ],
        "intervals": [
            {
                "kind": i.kind,
                "seq": i.seq,
                "duration_min": round(i.duration_s / 60, 2) if i.duration_s else None,
                "distance_km": round(i.distance_m / 1000, 3) if i.distance_m else None,
                "ascent_m": i.ascent_m,
                "avg_hr": i.avg_hr,
                "avg_speed_kmh": round(i.avg_speed_mps * 3.6, 2) if i.avg_speed_mps else None,
            }
            for i in intervals
        ],
        "streams": {
            "path": stream_set.storage_path if stream_set else None,
            "sample_count": stream_set.sample_count if stream_set else None,
            "channels": [
                {"channel": c.channel, "unit": c.unit, "min": c.min, "max": c.max, "mean": c.mean}
                for c in channels
            ],
        },
    }


def _pace_min_km(s: pl.Series) -> pl.Series:
    """Convert speed (m/s) to pace (min/km), capping very slow/stopped samples."""
    def _to_pace(v: float | None) -> float | None:
        if v is None or v <= 0:
            return None
        pace = 16.6666667 / v
        return None if pace > 20 else pace

    return s.map_elements(_to_pace, return_dtype=pl.Float64)


@app.get("/v1/activities/{activity_id}/timeseries")
def get_timeseries(
    activity_id: UUID,
    channels: str | None = None,
    max_points: int = 2000,
):
    """Return downsampled timeseries for an activity, with derived running channels."""
    from trailcoach.core.config import settings
    from trailcoach.training.grade import add_grade_to_dataframe, grade_adjusted_speed

    db = next(_get_db())
    stream_set = (
        db.query(ActivityStreamSet)
        .filter(ActivityStreamSet.activity_id == activity_id)
        .first()
    )
    if not stream_set:
        raise HTTPException(404, "No stream data")
    path = settings.processed_root_path / stream_set.storage_path
    if not path.exists():
        raise HTTPException(404, "Parquet file missing")
    df = pl.read_parquet(path)

    # Derive grade and grade-adjusted speed/pace on demand.
    df = add_grade_to_dataframe(df)
    if "speed_mps" in df.columns:
        gap = [
            grade_adjusted_speed(s, g)
            for s, g in zip(
                df["speed_mps"].to_list(), df["grade_pct"].to_list(), strict=False
            )
        ]
        df = df.with_columns(
            pl.Series("gap_mps", gap, dtype=pl.Float64),
            _pace_min_km(df["speed_mps"]).alias("pace_min_km"),
        )
        if "gap_mps" in df.columns:
            df = df.with_columns(_pace_min_km(df["gap_mps"]).alias("gap_pace_min_km"))

    available = [c for c in df.columns if c != "t_s"]

    requested = (channels or "").split(",")
    requested = [c.strip() for c in requested if c.strip()]
    if not requested:
        requested = ["altitude_m", "hr_bpm", "speed_mps", "grade_pct", "pace_min_km"]
    requested = [c for c in requested if c in df.columns]
    if not requested:
        requested = available[:5]

    if len(df) > max_points:
        step = max(1, len(df) // max_points)
        df = df[::step]

    return {
        "activity_id": str(activity_id),
        "channels": requested,
        "available": available,
        "count": len(df),
        "data": {c: df[c].to_list() for c in ["t_s"] + requested if c in df.columns},
    }


@app.get("/v1/weekly-volume")
def weekly_volume(weeks: int = 12):
    """Aggregated weekly volume for charts."""
    db = next(_get_db())
    today = date.today()
    start = datetime.combine(today - timedelta(weeks=weeks), datetime.min.time())
    activities = (
        db.query(Activity)
        .filter(Activity.start_time_utc >= start)
        .order_by(Activity.start_time_utc)
        .all()
    )
    from collections import defaultdict
    from typing import Any

    weeks_agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"duration_s": 0.0, "distance_m": 0.0, "ascent_m": 0.0, "n": 0}
    )
    for a in activities:
        if a.start_time_utc is None:
            continue
        # ISO calendar week: YYYY-WNN
        y, w, _ = a.start_time_utc.isocalendar()
        key = f"{y}-W{w:02d}"
        weeks_agg[key]["duration_s"] += float(a.duration_elapsed_s) if a.duration_elapsed_s else 0
        weeks_agg[key]["distance_m"] += float(a.distance_m) if a.distance_m else 0
        weeks_agg[key]["ascent_m"] += float(a.ascent_m) if a.ascent_m else 0
        weeks_agg[key]["n"] += 1
    return [
        {
            "week": key,
            "duration_h": round(v["duration_s"] / 3600, 2),
            "distance_km": round(v["distance_m"] / 1000, 2),
            "ascent_m": v["ascent_m"],
            "activities": v["n"],
        }
        for key, v in sorted(weeks_agg.items())
    ]


@app.get("/v1/pmc")
def get_pmc(from_date: date | None = None, to_date: date | None = None):
    """Performance Management Chart data (CTL/ATL/TSB)."""
    db = next(_get_db())
    q = db.query(TrainingMetricDaily).order_by(TrainingMetricDaily.date)
    if from_date:
        q = q.filter(TrainingMetricDaily.date >= from_date)
    if to_date:
        q = q.filter(TrainingMetricDaily.date <= to_date)
    rows = q.all()
    return [
        {
            "date": r.date.isoformat(),
            "ctl": r.ctl,
            "atl": r.atl,
            "tsb": r.tsb,
            "ctl_ascent": r.ctl_ascent,
            "atl_ascent": r.atl_ascent,
            "is_stale": r.is_stale,
        }
        for r in rows
    ]


@app.get("/v1/athlete-state")
def athlete_state():
    """Summarised athlete state for the AI Coach and dashboard."""
    with get_db() as db:
        athlete = db.query(Athlete).first()
        if athlete is None:
            raise HTTPException(404, "No athlete configured")
        return build_athlete_state(db, athlete)


class AskRequest(BaseModel):
    question: str
    provider: str | None = None


@app.post("/v1/ask")
def ask_question(req: AskRequest):
    """Ask the AI coach a question based on the Athlete State."""
    with get_db() as db:
        athlete = db.query(Athlete).first()
        if athlete is None:
            raise HTTPException(404, "No athlete configured")
        return ask_coach(db, athlete, req.question, req.provider)


@app.get("/v1/athlete-thresholds")
def list_thresholds():
    """Return the versioned threshold history for the configured athlete."""
    db = next(_get_db())
    athlete = db.query(Athlete).first()
    if not athlete:
        raise HTTPException(404, "No athlete configured")
    rows = (
        db.query(AthleteThreshold)
        .filter(AthleteThreshold.athlete_id == athlete.id)
        .order_by(AthleteThreshold.kind, AthleteThreshold.valid_from)
        .all()
    )
    return [
        {
            "kind": r.kind,
            "value": float(r.value),
            "unit": r.unit,
            "valid_from": r.valid_from.isoformat(),
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "source": r.source,
        }
        for r in rows
    ]


@app.get("/v1/weekly-load")
def weekly_load(weeks: int = 12):
    """Aggregated weekly load/volume from DailyLoad for charts."""
    from collections import defaultdict
    from typing import Any

    db = next(_get_db())
    athlete = db.query(Athlete).first()
    if not athlete:
        raise HTTPException(404, "No athlete configured")
    today = date.today()
    start = today - timedelta(weeks=weeks)
    rows = (
        db.query(DailyLoad)
        .filter(
            DailyLoad.athlete_id == athlete.id,
            DailyLoad.date >= start,
            DailyLoad.date <= today,
        )
        .order_by(DailyLoad.date)
        .all()
    )
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "load": 0.0,
            "distance_m": 0.0,
            "ascent_m": 0.0,
            "duration_s": 0.0,
            "n_activities": 0,
        }
    )
    for r in rows:
        y, w, _ = r.date.isocalendar()
        key = f"{y}-W{w:02d}"
        agg[key]["load"] += float(r.load_primary or 0)
        agg[key]["distance_m"] += float(r.distance_m or 0)
        agg[key]["ascent_m"] += float(r.ascent_m or 0)
        agg[key]["duration_s"] += float(r.duration_s or 0)
        agg[key]["n_activities"] += int(r.n_activities or 0)
    return [
        {
            "week": key,
            "load": round(v["load"], 2),
            "distance_km": round(v["distance_m"] / 1000, 2),
            "ascent_m": round(v["ascent_m"], 2),
            "duration_h": round(v["duration_s"] / 3600, 2),
            "activities": v["n_activities"],
        }
        for key, v in sorted(agg.items())
    ]
