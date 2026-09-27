"""Generic deduplication engine for source activities."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from trailcoach.db.models import (
    DedupeReview,
    SourceActivity,
    SourcePriority,
)


@dataclass
class DedupeScore:
    """Detailed similarity score between a candidate and an existing activity."""

    time_score: float
    distance_score: float
    duration_score: float
    source_bonus: float
    final_score: float


@dataclass
class DedupeAction:
    """Decision produced by the deduplication engine."""

    decision: str  # AUTO_LINK, REVIEW, SEPARATE, REIMPORT
    existing_source_activity_id: uuid.UUID | None = None
    existing_canonical_activity_id: uuid.UUID | None = None
    score: DedupeScore | None = None
    review_id: uuid.UUID | None = None
    reason: str | None = None


def _both_present(a: float | Decimal | None, b: float | Decimal | None) -> bool:
    """A component is comparable only when both records carry a positive value."""
    return a is not None and b is not None and float(a) > 0 and float(b) > 0


def _as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; stored values are always UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class DedupeEngine:
    """Provider-agnostic deduplication based on time, distance, duration and source priority.

    The engine never updates database objects directly in P0.0; it returns a
    DedupeAction so the orchestrator can commit links and reviews. This makes
    the engine testable and reusable for any source.

    Scoring: time/distance/duration similarities are combined with weights that
    are renormalized over the components both records actually carry, so a
    perfect match scores 1.0 whether or not the sport has distance (e.g. gym
    sessions). `source_bonus` is a small additive tiebreaker scaled by
    `source_weight`; it can never move a pair across a decision band on its own.
    """

    def __init__(
        self,
        time_tolerance_s: int = 600,
        distance_tolerance_pct: float = 0.10,
        duration_tolerance_pct: float = 0.10,
        time_weight: float = 0.35,
        distance_weight: float = 0.25,
        duration_weight: float = 0.25,
        source_weight: float = 0.15,
        auto_link_threshold: float = 0.85,
        review_threshold: float = 0.75,
    ) -> None:
        self.time_tolerance_s = time_tolerance_s
        self.distance_tolerance_pct = distance_tolerance_pct
        self.duration_tolerance_pct = duration_tolerance_pct
        self.time_weight = time_weight
        self.distance_weight = distance_weight
        self.duration_weight = duration_weight
        self.source_weight = source_weight
        self.auto_link_threshold = auto_link_threshold
        self.review_threshold = review_threshold

    def evaluate(self, db: Session, new_sa: SourceActivity) -> DedupeAction:
        """Evaluate a new SourceActivity against existing records for the athlete."""
        if new_sa.source_activity_id is None or new_sa.source is None:
            return DedupeAction(
                decision="SEPARATE",
                reason="new source activity is missing required identifiers",
            )

        existing = self._find_exact(db, new_sa)
        if existing:
            return DedupeAction(
                decision="REIMPORT",
                existing_source_activity_id=existing.id,
                existing_canonical_activity_id=existing.canonical_activity_id,
                reason=(
                    f"same source and source_activity_id "
                    f"{new_sa.source_activity_id!r} already exists"
                ),
            )

        candidates = self._find_candidates(db, new_sa)
        if not candidates:
            return DedupeAction(
                decision="SEPARATE",
                reason="no candidate within tolerance",
            )

        priority_map = self._priority_map(
            db, new_sa.athlete_id, metric="activity"
        )

        best: tuple[SourceActivity, DedupeScore] | None = None
        for candidate in candidates:
            score = self._score_pair(new_sa, candidate, priority_map)
            if best is None or score.final_score > best[1].final_score:
                best = (candidate, score)

        if best is None:
            return DedupeAction(decision="SEPARATE", reason="scoring produced no candidate")

        existing_sa, final_score = best

        if final_score.final_score >= self.auto_link_threshold:
            decision = "AUTO_LINK"
            review_id = None
            reason = "high similarity score"
        elif final_score.final_score >= self.review_threshold:
            decision = "REVIEW"
            review = DedupeReview(
                id=uuid.uuid4(),
                source_activity_id=new_sa.id or uuid.uuid4(),
                candidates={"best_candidate": str(existing_sa.id)},
                final_score=final_score.final_score,
                reason=(
                    f"Ambiguous match with {existing_sa.source} "
                    f"activity {existing_sa.source_activity_id}"
                ),
                status="pending",
            )
            db.add(review)
            review_id = review.id
            reason = review.reason or "ambiguous match"
        else:
            decision = "SEPARATE"
            review_id = None
            reason = "similarity below review threshold"

        return DedupeAction(
            decision=decision,
            existing_source_activity_id=existing_sa.id,
            existing_canonical_activity_id=existing_sa.canonical_activity_id,
            score=final_score,
            review_id=review_id,
            reason=reason,
        )

    def _find_exact(self, db: Session, new_sa: SourceActivity) -> SourceActivity | None:
        if new_sa.source is None or new_sa.source_activity_id is None:
            return None
        return (
            db.query(SourceActivity)
            .filter_by(
                athlete_id=new_sa.athlete_id,
                source=new_sa.source,
                source_activity_id=new_sa.source_activity_id,
            )
            .first()
        )

    def _find_candidates(
        self, db: Session, new_sa: SourceActivity
    ) -> list[SourceActivity]:
        if new_sa.start_time_utc is None:
            return []

        start_window = new_sa.start_time_utc - timedelta(seconds=self.time_tolerance_s)
        end_window = new_sa.start_time_utc + timedelta(seconds=self.time_tolerance_s)

        query = (
            db.query(SourceActivity)
            .filter(SourceActivity.athlete_id == new_sa.athlete_id)
            .filter(SourceActivity.start_time_utc.between(start_window, end_window))
        )

        if new_sa.id is not None:
            query = query.filter(SourceActivity.id != new_sa.id)

        if new_sa.sport_raw:
            query = query.filter(
                (SourceActivity.sport_raw == new_sa.sport_raw)
                | (SourceActivity.sport_raw.is_(None))
            )

        return query.all()

    def _priority_map(self, db: Session, athlete_id: uuid.UUID, metric: str) -> dict[str, int]:
        row = (
            db.query(SourcePriority)
            .filter_by(athlete_id=athlete_id, metric=metric)
            .first()
        )
        if row and row.priority:
            return {source: rank for rank, source in enumerate(row.priority)}
        return {}

    def _score_pair(
        self,
        new_sa: SourceActivity,
        existing_sa: SourceActivity,
        priority_map: dict[str, int],
    ) -> DedupeScore:
        time_score = self._time_score(new_sa, existing_sa)
        distance_score = self._distance_score(new_sa, existing_sa)
        duration_score = self._duration_score(new_sa, existing_sa)
        source_bonus = self._source_bonus(new_sa.source, existing_sa.source, priority_map)

        components = [(self.time_weight, time_score)]
        if _both_present(new_sa.distance_m, existing_sa.distance_m):
            components.append((self.distance_weight, distance_score))
        if _both_present(new_sa.duration_elapsed_s, existing_sa.duration_elapsed_s):
            components.append((self.duration_weight, duration_score))
        total_weight = sum(w for w, _ in components)
        similarity = sum(w * s for w, s in components) / total_weight

        final = max(0.0, min(1.0, similarity + self.source_weight * source_bonus))

        return DedupeScore(
            time_score=round(time_score, 4),
            distance_score=round(distance_score, 4),
            duration_score=round(duration_score, 4),
            source_bonus=round(source_bonus, 4),
            final_score=round(final, 4),
        )

    def _time_score(self, new_sa: SourceActivity, existing_sa: SourceActivity) -> float:
        if new_sa.start_time_utc is None or existing_sa.start_time_utc is None:
            return 0.0
        diff = abs(
            (_as_utc(new_sa.start_time_utc) - _as_utc(existing_sa.start_time_utc)).total_seconds()
        )
        return self._clamp(1.0 - diff / self.time_tolerance_s)

    def _distance_score(self, new_sa: SourceActivity, existing_sa: SourceActivity) -> float:
        if new_sa.distance_m is None or existing_sa.distance_m is None:
            return 0.0
        a = float(new_sa.distance_m)
        b = float(existing_sa.distance_m)
        max_distance = max(abs(a), abs(b), 1.0)
        tolerance = self.distance_tolerance_pct * max_distance
        diff = abs(a - b)
        return self._clamp(1.0 - diff / tolerance)

    def _duration_score(self, new_sa: SourceActivity, existing_sa: SourceActivity) -> float:
        if new_sa.duration_elapsed_s is None or existing_sa.duration_elapsed_s is None:
            return 0.0
        a = float(new_sa.duration_elapsed_s)
        b = float(existing_sa.duration_elapsed_s)
        max_duration = max(abs(a), abs(b), 1.0)
        tolerance = self.duration_tolerance_pct * max_duration
        diff = abs(a - b)
        return self._clamp(1.0 - diff / tolerance)

    def _source_bonus(
        self, new_source: str | None, existing_source: str | None, priority_map: dict[str, int]
    ) -> float:
        if not priority_map or not new_source or not existing_source:
            return 0.0
        if new_source not in priority_map or existing_source not in priority_map:
            return 0.0
        new_rank = priority_map[new_source]
        existing_rank = priority_map[existing_source]
        if new_rank < existing_rank:
            return 0.10
        if new_rank > existing_rank:
            return -0.05
        return 0.0

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))
