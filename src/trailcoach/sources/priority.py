"""Source-of-truth resolution helpers for multi-source metrics."""

from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.db.models import SourcePriority


def resolve_source_for_metric(
    db: Session,
    athlete_id: UUID,
    metric: str,
    available_sources: list[str],
) -> str | None:
    """Pick the highest-priority source that can provide a metric.

    This is intentionally separate from the deduplication `source_bonus`.
    Deduplication decides whether two records are the same activity;
    this helper decides which source's value wins for a given metric
    when multiple sources coexist.
    """
    if not available_sources:
        return None

    row = db.query(SourcePriority).filter_by(athlete_id=athlete_id, metric=metric).first()
    if not row or not row.priority:
        return available_sources[0]

    priority_index = {source: rank for rank, source in enumerate(row.priority)}

    def sort_key(source: str) -> int:
        return priority_index.get(source, len(row.priority))

    ordered = sorted(available_sources, key=sort_key)
    return ordered[0]
