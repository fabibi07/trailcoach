"""Rebuild derived source data from stored raw records.

Raw data first: when a parser learns to extract new information, existing
activities are updated from their immutable `RawFile` instead of being
re-imported. Only per-source summary metrics and zone times are rebuilt;
canonical activities, links and streams are left untouched.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from trailcoach.db.models import (
    ActivitySourceMetric,
    ActivityZoneTime,
    RawFile,
    SourceActivity,
)
from trailcoach.ingest.orchestrator import add_payload_metrics
from trailcoach.ingest.raw_store import RawStore
from trailcoach.sources.provider import SourceProvider


@dataclass
class ReprocessSummary:
    source: str
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    metrics: int = 0
    zone_times: int = 0
    errors: list[str] = field(default_factory=list)


def reprocess_activity_metrics(
    db: Session,
    provider: SourceProvider,
    athlete_id: uuid.UUID,
    raw_store: RawStore | None = None,
) -> ReprocessSummary:
    """Replace metrics/zone times of every linked source activity from its raw file."""
    store = raw_store or RawStore()
    summary = ReprocessSummary(source=provider.source)
    source_activities = (
        db.query(SourceActivity)
        .filter(
            SourceActivity.athlete_id == athlete_id,
            SourceActivity.source == provider.source,
            SourceActivity.canonical_activity_id.is_not(None),
        )
        .all()
    )
    for sa in source_activities:
        raw_id = sa.fit_raw_file_id or sa.summary_raw_file_id
        raw = db.get(RawFile, raw_id) if raw_id else None
        if raw is None or sa.canonical_activity_id is None:
            summary.skipped += 1
            continue
        savepoint = db.begin_nested()
        try:
            payload = provider.payload_from_raw(store.read_bytes(raw), sa.source_activity_id)
            if payload is None:
                summary.skipped += 1
                savepoint.commit()
                continue
            db.query(ActivitySourceMetric).filter_by(source_activity_id=sa.id).delete()
            db.query(ActivityZoneTime).filter_by(source_activity_id=sa.id).delete()
            n_metrics, n_zones = add_payload_metrics(db, payload, sa, sa.canonical_activity_id)
            db.flush()
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - one bad raw file must not abort the batch
            savepoint.rollback()
            summary.failed += 1
            summary.errors.append(f"{sa.source_activity_id}: {exc}")
            continue
        summary.processed += 1
        summary.metrics += n_metrics
        summary.zone_times += n_zones
    return summary
