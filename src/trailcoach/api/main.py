"""FastAPI application."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from trailcoach.db.models import (
    Activity,
    ActivityInterval,
    ActivityStreamChannel,
    ActivityStreamSet,
    Athlete,
    SourceActivity,
    TrainingMetricDaily,
)
from trailcoach.db.session import SessionLocal


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


@app.get("/v1/activities/{activity_id}/timeseries")
def get_timeseries(activity_id: UUID, channels: str | None = None, max_points: int = 500):
    """Return downsampled timeseries for an activity."""
    import polars as pl

    from trailcoach.core.config import settings

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
    requested = (channels or ",").split(",")
    requested = [c for c in requested if c and c in df.columns]
    if not requested:
        requested = [c for c in df.columns if c != "t_s"]
    if len(df) > max_points:
        step = max(1, len(df) // max_points)
        df = df[::step]
    return {
        "activity_id": str(activity_id),
        "channels": requested,
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
            "is_stale": r.is_stale,
        }
        for r in rows
    ]
