"""Versioned athlete thresholds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import UUID

import numpy as np
import polars as pl
from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import (
    Activity,
    ActivitySourceMetric,
    ActivityStreamSet,
    AthleteThreshold,
    ThresholdEstimate,
    WellnessDaily,
)
from trailcoach.sources.priority import resolve_source_for_metric
from trailcoach.training.threshold_estimation import (
    DEVICE_METRICS,
    ActivityEvidence,
    Candidate,
    EstimationResult,
    estimate,
    hr_summary,
)

AUTO_SOURCES = ("derived", "device")


def get_thresholds(
    db: Session,
    athlete_id: UUID,
    as_of: date,
) -> dict[str, AthleteThreshold]:
    """Return the latest active threshold for each kind on a given date."""
    rows = (
        db.query(AthleteThreshold)
        .filter(
            AthleteThreshold.athlete_id == athlete_id,
            AthleteThreshold.valid_from <= as_of,
            (AthleteThreshold.valid_to.is_(None)) | (AthleteThreshold.valid_to >= as_of),
        )
        .order_by(AthleteThreshold.kind, AthleteThreshold.valid_from.desc())
        .all()
    )
    latest: dict[str, AthleteThreshold] = {}
    for row in rows:
        if row.kind not in latest:
            latest[row.kind] = row
    return latest


def get_threshold_value(
    db: Session,
    athlete_id: UUID,
    kind: str,
    as_of: date,
    default: float | None = None,
) -> float | None:
    """Return a single threshold value as a float, or default if missing."""
    threshold = (
        db.query(AthleteThreshold)
        .filter(
            AthleteThreshold.athlete_id == athlete_id,
            AthleteThreshold.kind == kind,
            AthleteThreshold.valid_from <= as_of,
            (AthleteThreshold.valid_to.is_(None)) | (AthleteThreshold.valid_to >= as_of),
        )
        .order_by(AthleteThreshold.valid_from.desc())
        .first()
    )
    if threshold is None:
        return default
    return float(threshold.value)


def set_threshold(
    db: Session,
    athlete_id: UUID,
    kind: str,
    value: float,
    unit: str,
    valid_from: date,
    source: str = "manual",
    notes: str | None = None,
    confidence: float | None = None,
) -> AthleteThreshold:
    """Insert a new versioned threshold and close the previous open one."""
    prev = (
        db.query(AthleteThreshold)
        .filter(
            AthleteThreshold.athlete_id == athlete_id,
            AthleteThreshold.kind == kind,
            AthleteThreshold.valid_to.is_(None),
            AthleteThreshold.valid_from < valid_from,
        )
        .order_by(AthleteThreshold.valid_from.desc())
        .first()
    )
    if prev is not None:
        prev.valid_to = valid_from - timedelta(days=1)

    threshold = AthleteThreshold(
        athlete_id=athlete_id,
        kind=kind,
        valid_from=valid_from,
        value=value,
        unit=unit,
        source=source,
        notes=notes,
        confidence=confidence,
    )
    db.add(threshold)
    db.flush()
    return threshold


# ---------------------------------------------------------------------------
# Estimación automática (P0.3)
# ---------------------------------------------------------------------------
@dataclass
class ThresholdApplySummary:
    result: EstimationResult
    written: list[Candidate] = field(default_factory=list)
    blocked_by_manual: int = 0
    applied: bool = False


def _activity_day(activity: Activity) -> date:
    return (activity.start_time_local or activity.start_time_utc).date()


def _read_stream(path: str) -> pl.DataFrame | None:
    full = settings.processed_root_path / path
    if not full.exists():
        return None
    df = pl.read_parquet(full)
    if "t_s" not in df.columns or "hr_bpm" not in df.columns:
        return None
    if "speed_mps" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("speed_mps"))
    return df.select("t_s", "hr_bpm", "speed_mps").drop_nulls(["t_s", "hr_bpm"]).sort("t_s")


def collect_threshold_inputs(
    db: Session, athlete_id: UUID
) -> tuple[list[ActivityEvidence], dict[date, float]]:
    """Reúne la evidencia por actividad y la FC de reposo diaria del atleta."""
    activities = db.query(Activity).filter(Activity.athlete_id == athlete_id).all()
    device: dict[UUID, dict[str, float]] = {}
    rows = (
        db.query(
            ActivitySourceMetric.activity_id,
            ActivitySourceMetric.metric,
            ActivitySourceMetric.value,
        )
        .join(Activity, Activity.id == ActivitySourceMetric.activity_id)
        .filter(
            Activity.athlete_id == athlete_id,
            ActivitySourceMetric.metric.in_(list(DEVICE_METRICS.values())),
        )
        .all()
    )
    for activity_id, metric, value in rows:
        device.setdefault(activity_id, {})[metric] = float(value)
    streams: dict[UUID, str] = {}
    for ss in (
        db.query(ActivityStreamSet)
        .join(Activity, Activity.id == ActivityStreamSet.activity_id)
        .filter(Activity.athlete_id == athlete_id)
        .order_by(ActivityStreamSet.created_at)
    ):
        streams.setdefault(ss.activity_id, ss.storage_path)

    evidence: list[ActivityEvidence] = []
    for activity in activities:
        ev = ActivityEvidence(
            day=_activity_day(activity),
            sport=activity.sport,
            sub_sport=activity.sub_sport,
            device=device.get(activity.id, {}),
        )
        path = streams.get(activity.id)
        df = _read_stream(path) if ev.is_run and path else None
        if df is not None and df.height > 1:
            t = df["t_s"].to_numpy().astype(float)
            hr = df["hr_bpm"].to_numpy().astype(float)
            ev.max_hr_30s, ev.best_hr_20min = hr_summary(t, hr)
            if ev.is_road_run:
                ev.t_s, ev.hr_bpm = t, hr
                ev.speed_mps = df["speed_mps"].fill_null(np.nan).to_numpy().astype(float)
        evidence.append(ev)

    wellness = (
        db.query(WellnessDaily.date, WellnessDaily.source, WellnessDaily.resting_hr)
        .filter(WellnessDaily.athlete_id == athlete_id, WellnessDaily.resting_hr.isnot(None))
        .all()
    )
    source = resolve_source_for_metric(
        db, athlete_id, "resting_hr", sorted({r.source for r in wellness})
    )
    resting = {r.date: float(r.resting_hr) for r in wellness if r.source == source}
    return evidence, resting


def estimate_and_apply(db: Session, athlete_id: UUID, apply: bool = False) -> ThresholdApplySummary:
    """Estima umbrales y, con `apply`, guarda candidatos y umbrales efectivos.

    Es idempotente: reemplaza solo lo generado automáticamente (`AUTO_SOURCES`).
    Un umbral manual (u otro origen) gana desde su `valid_from` en adelante.
    """
    result = estimate(*collect_threshold_inputs(db, athlete_id))
    summary = ThresholdApplySummary(result=result)
    manual_rows = (
        db.query(AthleteThreshold)
        .filter(
            AthleteThreshold.athlete_id == athlete_id,
            AthleteThreshold.source.notin_(AUTO_SOURCES),
        )
        .all()
    )
    manual_first: dict[str, date] = {}
    for row in manual_rows:
        if row.kind not in manual_first or row.valid_from < manual_first[row.kind]:
            manual_first[row.kind] = row.valid_from
    by_kind: dict[str, list[Candidate]] = {}
    for c in result.effective:
        if c.valid_from >= manual_first.get(c.kind, date.max):
            summary.blocked_by_manual += 1
            continue
        by_kind.setdefault(c.kind, []).append(c)
    summary.written = [c for kind in sorted(by_kind) for c in by_kind[kind]]
    if not apply:
        return summary

    db.query(ThresholdEstimate).filter(ThresholdEstimate.athlete_id == athlete_id).delete(
        synchronize_session=False
    )
    db.query(AthleteThreshold).filter(
        AthleteThreshold.athlete_id == athlete_id, AthleteThreshold.source.in_(AUTO_SOURCES)
    ).delete(synchronize_session=False)
    for c in result.candidates:
        db.add(
            ThresholdEstimate(
                athlete_id=athlete_id,
                kind=c.kind,
                valid_from=c.valid_from,
                value=c.value,
                unit=c.unit,
                method=c.method,
                source=c.source,
                confidence=c.confidence,
                evidence=c.evidence,
            )
        )
    for kind, rows_k in by_kind.items():
        for i, c in enumerate(rows_k):
            if i + 1 < len(rows_k):
                valid_to: date | None = rows_k[i + 1].valid_from - timedelta(days=1)
            elif kind in manual_first:
                valid_to = manual_first[kind] - timedelta(days=1)
            else:
                valid_to = None
            db.add(
                AthleteThreshold(
                    athlete_id=athlete_id,
                    kind=kind,
                    valid_from=c.valid_from,
                    valid_to=valid_to,
                    value=c.value,
                    unit=c.unit,
                    source=c.source,
                    confidence=c.confidence,
                    notes=c.method,
                )
            )
    db.flush()
    summary.applied = True
    return summary
