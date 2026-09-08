"""Versioned athlete thresholds."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.db.models import AthleteThreshold


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
