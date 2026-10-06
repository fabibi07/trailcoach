"""Abstract contract for all source adapters in TrailCoach."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trailcoach.db.models import (
    Activity,
    ActivityInterval,
    ActivitySourceMetric,
    ActivityZoneTime,
    AthleteSourceAccount,
    MetricCapability,
    RawFile,
    SourceActivity,
    WellnessDaily,
)


@dataclass
class HistoricalRange:
    """Date range available for a given source account."""

    start: date | None = None
    end: date | None = None


@dataclass
class ActivityPayload:
    """Source-agnostic side data for one imported activity.

    Everything here is optional: an aggregator may only provide a summary,
    a device export may provide the original file plus full time series.
    `stream_rows` must use canonical channel names (`trailcoach.ingest.streams`).
    `metrics` and `zone_times` are detached rows whose activity ids are set by
    the orchestrator once the canonical activity is known.
    """

    source_activity_id: str
    raw_content: bytes | None = None
    raw_kind: str = "file"
    raw_extension: str = ""
    raw_content_type: str | None = None
    stream_rows: list[dict[str, Any]] = field(default_factory=list)
    intervals: list[ActivityInterval] = field(default_factory=list)
    device: dict[str, str | None] | None = None
    metrics: list[ActivitySourceMetric] = field(default_factory=list)
    zone_times: list[ActivityZoneTime] = field(default_factory=list)


@dataclass
class ImportResult:
    """Result returned by a source provider import/sync operation.

    The provider never commits to the database itself; it returns detached
    or newly created model instances and lets the ingestion orchestrator
    persist them. This keeps providers stateless and testable.

    `source_activities[i]` and `activities[i]` describe the same record when
    both lists are populated. `payloads` is keyed by `source_activity_id`.
    """

    source: str
    start: date | None = None
    end: date | None = None
    source_activities: list[SourceActivity] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    wellness_records: list[WellnessDaily] = field(default_factory=list)
    raw_files: list[RawFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    capabilities: list[MetricCapability] = field(default_factory=list)
    payloads: dict[str, ActivityPayload] = field(default_factory=dict)


class SourceProvider(ABC):
    """Generic adapter for a device, platform or aggregator.

    Implementations are discovered through SourceProviderRegistry and are
    agnostic of the TrailCoach database session.
    """

    source: str

    @abstractmethod
    def authenticate(self, account: AthleteSourceAccount) -> dict[str, Any]:
        """Validate or refresh credentials for the account.

        For file-based sources this can be a no-op that returns an empty
        status dictionary.
        """

    @abstractmethod
    def discover_capabilities(
        self, account: AthleteSourceAccount
    ) -> list[MetricCapability]:
        """Return the metrics this source can provide for this account."""

    @abstractmethod
    def get_historical_range(self, account: AthleteSourceAccount) -> HistoricalRange:
        """Return the inclusive date range of data available to import."""

    @abstractmethod
    def initial_import(
        self,
        account: AthleteSourceAccount,
        start: date | None = None,
        end: date | None = None,
    ) -> ImportResult:
        """Import historical data for the account.

        The provider must not assume it is the first/only source. Duplicates
        are resolved later by the deduplication engine.
        """

    @abstractmethod
    def incremental_sync(self, account: AthleteSourceAccount) -> ImportResult:
        """Import only data changed since the last sync cursor."""

    @abstractmethod
    def download_activity_file(
        self, account: AthleteSourceAccount, source_activity_id: str
    ) -> bytes | None:
        """Download the raw activity file when the source supports it."""

    def payload_from_raw(
        self, content: bytes, source_activity_id: str
    ) -> ActivityPayload | None:
        """Rebuild an `ActivityPayload` from a stored raw record.

        Enables reprocessing raw data with a newer parser without
        re-importing. Sources that cannot reparse their raw data return None.
        """
        return None

    @abstractmethod
    def get_wellness(
        self, account: AthleteSourceAccount, start: date, end: date
    ) -> list[WellnessDaily]:
        """Return daily wellness records in the requested date range."""
