"""Training engine orchestration: loads -> daily load -> PMC."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import (
    Activity,
    ActivityLoad,
    ActivityStreamSet,
    Athlete,
    DailyLoad,
    TrainingMetricDaily,
)
from trailcoach.training.load import MODEL_VERSION, compute_activity_load
from trailcoach.training.pmc import recalculate_pmc
from trailcoach.training.thresholds import get_thresholds


def _to_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"Cannot convert {value!r} to date")


def _recompute_activity_loads(
    db: Session,
    athlete_id: UUID,
    athlete: Athlete,
    from_date: date | None,
) -> None:
    q = db.query(Activity).filter(Activity.athlete_id == athlete_id)
    if from_date:
        q = q.filter(func.date(Activity.start_time_utc) >= from_date)
    activities = q.order_by(Activity.start_time_utc).all()

    for activity in activities:
        stream_set = (
            db.query(ActivityStreamSet)
            .filter(ActivityStreamSet.activity_id == activity.id)
            .first()
        )
        if stream_set is None:
            continue
        stream_path = settings.processed_root_path / stream_set.storage_path
        if not stream_path.exists():
            continue

        thresholds = get_thresholds(db, athlete_id, activity.start_time_utc.date())
        loads = compute_activity_load(activity, stream_path, thresholds, athlete)

        db.query(ActivityLoad).filter(
            ActivityLoad.activity_id == activity.id,
            ActivityLoad.model_version == MODEL_VERSION,
        ).delete(synchronize_session=False)

        for _method, result in loads.items():
            db.add(
                ActivityLoad(
                    activity_id=activity.id,
                    method=result.method,
                    value=result.value,
                    is_primary=result.is_primary,
                    selection_reason="primary method" if result.is_primary else None,
                    threshold_refs=result.threshold_refs,
                    model_version=MODEL_VERSION,
                    inputs_hash=result.inputs_hash,
                )
            )
        db.flush()


def _recompute_daily_loads(
    db: Session,
    athlete_id: UUID,
    from_date: date | None,
) -> None:
    activity_filter = [Activity.athlete_id == athlete_id]
    if from_date:
        activity_filter.append(func.date(Activity.start_time_utc) >= from_date)

    primary_rows = (
        db.query(
            func.date(Activity.start_time_utc).label("day"),
            func.sum(ActivityLoad.value).label("load_primary"),
        )
        .join(Activity, Activity.id == ActivityLoad.activity_id)
        .filter(*activity_filter, ActivityLoad.is_primary.is_(True))
        .group_by(func.date(Activity.start_time_utc))
        .all()
    )

    method_rows = (
        db.query(
            func.date(Activity.start_time_utc).label("day"),
            ActivityLoad.method,
            func.sum(ActivityLoad.value).label("value"),
        )
        .join(Activity, Activity.id == ActivityLoad.activity_id)
        .filter(*activity_filter)
        .group_by(func.date(Activity.start_time_utc), ActivityLoad.method)
        .all()
    )

    summary_rows = (
        db.query(
            func.date(Activity.start_time_utc).label("day"),
            func.sum(Activity.distance_m).label("distance_m"),
            func.sum(Activity.ascent_m).label("ascent_m"),
            func.sum(Activity.descent_m).label("descent_m"),
            func.sum(func.coalesce(Activity.duration_moving_s, Activity.duration_elapsed_s)).label(
                "duration_s"
            ),
            func.count(Activity.id).label("n_activities"),
            func.max(
                case((Activity.data_quality == "degraded", 1), else_=0)
            ).label("has_degraded"),
        )
        .filter(*activity_filter)
        .group_by(func.date(Activity.start_time_utc))
        .all()
    )

    daily_map: dict[date, dict[str, Any]] = defaultdict(
        lambda: {
            "load_primary": 0.0,
            "load_by_method": {},
            "distance_m": 0.0,
            "ascent_m": 0.0,
            "descent_m": 0.0,
            "duration_s": 0.0,
            "n_activities": 0,
            "has_degraded": False,
        }
    )

    for row in primary_rows:
        d = _to_date(row.day)
        daily_map[d]["load_primary"] += float(row.load_primary or 0.0)

    for row in method_rows:
        d = _to_date(row.day)
        daily_map[d]["load_by_method"][row.method] = (
            daily_map[d]["load_by_method"].get(row.method, 0.0) + float(row.value or 0.0)
        )

    for row in summary_rows:
        d = _to_date(row.day)
        daily_map[d]["distance_m"] = float(row.distance_m or 0.0)
        daily_map[d]["ascent_m"] = float(row.ascent_m or 0.0)
        daily_map[d]["descent_m"] = float(row.descent_m or 0.0)
        daily_map[d]["duration_s"] = float(row.duration_s or 0.0)
        daily_map[d]["n_activities"] = int(row.n_activities or 0)
        daily_map[d]["has_degraded"] = bool(row.has_degraded)

    if not daily_map:
        return

    start = min(daily_map)
    db.query(DailyLoad).filter(
        DailyLoad.athlete_id == athlete_id,
        DailyLoad.date >= start,
    ).delete(synchronize_session=False)

    for d, vals in daily_map.items():
        vertical_work = vals["ascent_m"] * 9.80665
        db.add(
            DailyLoad(
                athlete_id=athlete_id,
                date=d,
                load_primary=round(vals["load_primary"], 3),
                load_by_method=vals["load_by_method"],
                duration_s=round(vals["duration_s"], 3),
                distance_m=round(vals["distance_m"], 3),
                ascent_m=round(vals["ascent_m"], 3),
                descent_m=round(vals["descent_m"], 3),
                vertical_work_j_per_kg=round(vertical_work, 3),
                n_activities=vals["n_activities"],
                has_degraded=vals["has_degraded"],
            )
        )
    db.flush()


def recalculate_athlete(
    db: Session,
    athlete_id: UUID,
    from_date: date | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Recompute activity loads, daily loads and PMC for an athlete."""
    athlete = db.query(Athlete).filter(Athlete.id == athlete_id).first()
    if athlete is None:
        raise ValueError(f"Athlete {athlete_id} not found")

    _recompute_activity_loads(db, athlete_id, athlete, from_date)
    _recompute_daily_loads(db, athlete_id, from_date)
    recalculate_pmc(db, athlete_id, from_date)

    if commit:
        db.commit()

    n_daily = db.query(DailyLoad).filter(DailyLoad.athlete_id == athlete_id).count()
    n_metrics = db.query(TrainingMetricDaily).filter(
        TrainingMetricDaily.athlete_id == athlete_id
    ).count()
    return {
        "athlete_id": str(athlete_id),
        "daily_load_rows": n_daily,
        "training_metric_rows": n_metrics,
    }
