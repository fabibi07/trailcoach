"""Provider-agnostic ingestion orchestrator.

Runs one `SourceProvider` for one `AthleteSourceAccount` and owns every
side effect the provider is not allowed to perform:

    provider.import ─▶ raw store ─▶ dedup ─▶ device/capabilities ─▶ persist
                                        │
                                        └─▶ DedupeReview (ambiguous)

Provenance is written as `DataLineage` on both `SourceActivity` and
`Activity`, and `AthleteSourceAccount` is the only place connection state
(cursor, health, timestamps) is updated.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from trailcoach.db.models import (
    Activity,
    ActivityLink,
    AthleteSourceAccount,
    DedupeReview,
    Device,
    MetricCapability,
    SourceActivity,
    WellnessDaily,
)
from trailcoach.dedup.engine import DedupeAction, DedupeEngine
from trailcoach.ingest.raw_store import RawStore
from trailcoach.ingest.streams import write_stream_parquet
from trailcoach.provenance import DataLineage
from trailcoach.sources.provider import ActivityPayload, ImportResult, SourceProvider


@dataclass
class IngestSummary:
    source: str
    mode: str
    created: int = 0
    linked: int = 0
    reviewed: int = 0
    reimported: int = 0
    failed: int = 0
    wellness: int = 0
    errors: list[str] = field(default_factory=list)
    activity_ids: list[uuid.UUID] = field(default_factory=list)
    review_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.created + self.linked + self.reviewed + self.reimported + self.failed


class IngestionOrchestrator:
    def __init__(
        self,
        db: Session,
        raw_store: RawStore | None = None,
        dedupe: DedupeEngine | None = None,
    ) -> None:
        self.db = db
        self.raw_store = raw_store or RawStore()
        self.dedupe = dedupe or DedupeEngine()

    # ------------------------------------------------------------- public

    def run(
        self,
        account: AthleteSourceAccount,
        provider: SourceProvider,
        mode: str = "initial",
        start: date | None = None,
        end: date | None = None,
    ) -> IngestSummary:
        if provider.source != account.source:
            raise ValueError(
                f"provider source {provider.source!r} does not match account {account.source!r}"
            )
        summary = IngestSummary(source=provider.source, mode=mode)
        now = datetime.now(timezone.utc)

        auth = provider.authenticate(account)
        account.auth_status = str(auth.get("auth_status", "unknown"))
        if auth.get("status") != "ok":
            self._mark_failure(account, now)
            summary.errors.append(str(auth.get("error", "authentication failed")))
            self.db.flush()
            return summary

        if mode == "incremental":
            result = provider.incremental_sync(account)
        else:
            result = provider.initial_import(account, start=start, end=end)

        summary.errors.extend(result.errors)
        self._upsert_capabilities(result.capabilities)

        activities = self._pair_activities(result)
        for sa, activity in zip(result.source_activities, activities, strict=True):
            payload = result.payloads.get(sa.source_activity_id)
            savepoint = self.db.begin_nested()
            try:
                self._ingest_activity(account, provider, sa, activity, payload, summary)
                savepoint.commit()
            except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
                savepoint.rollback()
                summary.failed += 1
                summary.errors.append(f"{sa.source_activity_id}: {exc}")

        for record in result.wellness_records:
            self._upsert_wellness(account, record)
            summary.wellness += 1

        if result.cursor:
            account.cursor_json = {**(account.cursor_json or {}), **result.cursor}
        if summary.failed and summary.failed == len(result.source_activities):
            self._mark_failure(account, now)
        else:
            account.health = "ok" if not summary.failed else "degraded"
            account.last_success_at = now
            account.consecutive_failures = 0
        self.db.flush()
        return summary

    # ------------------------------------------------------------ private

    @staticmethod
    def _pair_activities(result: ImportResult) -> list[Activity | None]:
        if len(result.activities) == len(result.source_activities):
            return list(result.activities)
        return [None] * len(result.source_activities)

    def _ingest_activity(
        self,
        account: AthleteSourceAccount,
        provider: SourceProvider,
        sa: SourceActivity,
        activity: Activity | None,
        payload: ActivityPayload | None,
        summary: IngestSummary,
    ) -> None:
        sa.athlete_id = account.athlete_id
        sa.account_id = account.id
        if sa.id is None:
            sa.id = uuid.uuid4()

        action = self.dedupe.evaluate(self.db, sa)
        if action.decision == "REIMPORT":
            summary.reimported += 1
            return
        self.db.add(sa)

        device = self._upsert_device(account, payload.device if payload else None)
        if device is not None:
            sa.device_id = device.id

        raw_file_id = self._store_raw(account, provider, sa, payload)
        if raw_file_id is not None:
            sa.fit_raw_file_id = raw_file_id
            sa.summary_raw_file_id = raw_file_id

        if sa.start_time_utc is None:
            raise ValueError("source activity has no start time")

        lineage = DataLineage(
            source=sa.source,
            timestamp=sa.start_time_utc,
            source_record_id=sa.source_activity_id,
            raw_file_id=raw_file_id,
            device_id=sa.device_id,
            account_id=account.id,
            metric="activity",
            value_type="native",
            confidence=1.0,
            data_quality=sa.data_quality,
        )
        sa.provenance = {**(sa.provenance or {}), **lineage.to_dict()}
        self.db.flush()

        if action.decision == "AUTO_LINK" and action.existing_canonical_activity_id:
            canonical_id = action.existing_canonical_activity_id
            sa.canonical_activity_id = canonical_id
            self._link(sa, canonical_id, "dedupe_auto", action)
            summary.linked += 1
        else:
            if activity is None:
                raise ValueError("provider returned no canonical Activity for source activity")
            activity.athlete_id = account.athlete_id
            activity.provenance = {**(activity.provenance or {}), **lineage.to_dict()}
            if action.decision == "REVIEW":
                activity.quality_flags = [*(activity.quality_flags or []), "dedupe_review_pending"]
            self.db.add(activity)
            self.db.flush()
            canonical_id = activity.id
            sa.canonical_activity_id = canonical_id
            self._link(sa, canonical_id, "primary", action)
            summary.activity_ids.append(canonical_id)
            if action.decision == "REVIEW" and action.review_id:
                self._enrich_review(action)
                summary.review_ids.append(action.review_id)
                summary.reviewed += 1
            else:
                summary.created += 1

        if payload is not None:
            if payload.stream_rows:
                write_stream_parquet(
                    self.db,
                    activity_id=canonical_id,
                    source_activity_id=sa.id,
                    rows=payload.stream_rows,
                    source=sa.source,
                    sha256=(sa.provenance or {}).get("raw_sha256"),
                    version=str((sa.provenance or {}).get("parser_version", "")),
                )
            for interval in payload.intervals:
                interval.activity_id = canonical_id
                self.db.add(interval)
        self.db.flush()

    def _link(
        self, sa: SourceActivity, canonical_id: uuid.UUID, method: str, action: DedupeAction
    ) -> None:
        confidence = action.score.final_score if action.score else 1.0
        evidence: dict[str, Any] = {"decision": action.decision, "reason": action.reason}
        if action.score:
            evidence["score"] = asdict(action.score)
        if action.existing_source_activity_id:
            evidence["existing_source_activity_id"] = str(action.existing_source_activity_id)
        self.db.add(
            ActivityLink(
                canonical_activity_id=canonical_id,
                source_activity_id=sa.id,
                match_method=method,
                confidence=confidence,
                evidence=evidence,
            )
        )

    def _enrich_review(self, action: DedupeAction) -> None:
        review = self.db.get(DedupeReview, action.review_id)
        if review is None or action.score is None:
            return
        review.candidates = {
            **(review.candidates or {}),
            "scores": asdict(action.score),
            "existing_canonical_activity_id": (
                str(action.existing_canonical_activity_id)
                if action.existing_canonical_activity_id
                else None
            ),
        }

    def _store_raw(
        self,
        account: AthleteSourceAccount,
        provider: SourceProvider,
        sa: SourceActivity,
        payload: ActivityPayload | None,
    ) -> uuid.UUID | None:
        content = payload.raw_content if payload else None
        kind = payload.raw_kind if payload else "file"
        extension = payload.raw_extension if payload else ""
        content_type = payload.raw_content_type if payload else None
        if content is None:
            content = provider.download_activity_file(account, sa.source_activity_id)
        if content is None:
            return None
        raw = self.raw_store.store(
            self.db,
            content=content,
            source=sa.source,
            kind=kind,
            athlete_id=account.athlete_id,
            source_activity_id=sa.source_activity_id,
            content_type=content_type,
            extension=extension,
            captured_at=sa.start_time_utc,
        )
        raw.parse_status = "ok"
        raw.parser_version = str((sa.provenance or {}).get("parser_version") or "")
        return raw.id

    def _upsert_device(
        self, account: AthleteSourceAccount, info: dict[str, str | None] | None
    ) -> Device | None:
        if not info or not any(info.values()):
            return None
        query = self.db.query(Device).filter_by(
            athlete_id=account.athlete_id,
            serial_number=info.get("serial_number"),
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
        )
        device = query.first()
        if device is None:
            device = Device(
                athlete_id=account.athlete_id,
                serial_number=info.get("serial_number"),
                manufacturer=info.get("manufacturer"),
                model=info.get("model"),
                name=" ".join(
                    str(p) for p in [info.get("manufacturer"), info.get("model")] if p
                )
                or None,
            )
            self.db.add(device)
            self.db.flush()
        if account.device_id is None:
            account.device_id = device.id
        return device

    def _upsert_capabilities(self, capabilities: list[MetricCapability]) -> None:
        for cap in capabilities:
            existing = (
                self.db.query(MetricCapability)
                .filter_by(source=cap.source, metric=cap.metric)
                .first()
            )
            if existing is None:
                self.db.add(cap)
            else:
                existing.availability = cap.availability
                existing.confidence_base = cap.confidence_base
                existing.notes = cap.notes
        self.db.flush()

    def _upsert_wellness(self, account: AthleteSourceAccount, record: WellnessDaily) -> None:
        record.athlete_id = account.athlete_id
        record.source = account.source
        self.db.merge(record)

    @staticmethod
    def _mark_failure(account: AthleteSourceAccount, now: datetime) -> None:
        account.health = "error"
        account.last_error_at = now
        account.consecutive_failures = (account.consecutive_failures or 0) + 1
