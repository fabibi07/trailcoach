"""Mock source provider for contract validation and synthetic tests."""

from datetime import date, datetime, timedelta, timezone

from trailcoach.db.models import (
    Activity,
    AthleteSourceAccount,
    MetricCapability,
    SourceActivity,
    WellnessDaily,
)
from trailcoach.sources.provider import HistoricalRange, ImportResult, SourceProvider
from trailcoach.sources.registry import SourceProviderRegistry


@SourceProviderRegistry.register
class MockSourceProvider(SourceProvider):
    """A purely synthetic source adapter.

    It never connects to external APIs and it never parses real files.
    The source name can be overridden when instantiated so tests can
    validate multi-source scenarios using generic slugs such as `source_a`
    and `source_b`.
    """

    source = "mock"

    def __init__(self, source: str | None = None) -> None:
        self.source = source or self.source

    def authenticate(self, account: AthleteSourceAccount) -> dict:
        """No-op authentication for the mock provider."""
        return {"status": "ok", "authentication_required": False, "source_type": "synthetic"}

    def discover_capabilities(
        self, account: AthleteSourceAccount
    ) -> list[MetricCapability]:
        """Return a controlled set of capabilities for contract tests."""
        metrics = [
            ("heart_rate", "native"),
            ("distance", "native"),
            ("duration", "native"),
            ("elevation_gain", "native"),
            ("cadence", "native"),
            ("pace", "derived"),
            ("ctl", "derived"),
            ("sleep_score", "unavailable"),
        ]
        return [
            MetricCapability(
                source=self.source,
                metric=metric,
                availability=availability,
                confidence_base=0.95 if availability == "native" else None,
            )
            for metric, availability in metrics
        ]

    def get_historical_range(self, account: AthleteSourceAccount) -> HistoricalRange:
        """Return a deterministic synthetic date range."""
        return HistoricalRange(start=date(2024, 1, 1), end=date(2024, 1, 7))

    def initial_import(
        self,
        account: AthleteSourceAccount,
        start: date | None = None,
        end: date | None = None,
    ) -> ImportResult:
        """Generate a small set of synthetic activities and wellness records."""
        source_activities = [
            SourceActivity(
                athlete_id=account.athlete_id,
                source=self.source,
                source_activity_id=f"{self.source}-001",
                start_time_utc=datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
                distance_m=12000.0,
                duration_elapsed_s=3600.0,
                duration_moving_s=3540.0,
                sport_raw="running",
                status="normalized",
                data_quality="authoritative",
            ),
            SourceActivity(
                athlete_id=account.athlete_id,
                source=self.source,
                source_activity_id=f"{self.source}-002",
                start_time_utc=datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc),
                distance_m=8000.0,
                duration_elapsed_s=2400.0,
                sport_raw="running",
                status="normalized",
                data_quality="authoritative",
            ),
        ]

        activities = [
            Activity(
                athlete_id=account.athlete_id,
                start_time_utc=sa.start_time_utc,
                sport=sa.sport_raw or "unknown",
                primary_source=self.source,
                distance_m=sa.distance_m,
                duration_elapsed_s=sa.duration_elapsed_s,
                duration_moving_s=sa.duration_moving_s,
                data_quality="authoritative",
            )
            for sa in source_activities
        ]

        for sa, activity in zip(source_activities, activities, strict=True):
            sa.canonical_activity_id = activity.id
            sa.provenance = {
                "source": self.source,
                "source_activity_id": sa.source_activity_id,
            }
            activity.provenance = {
                "source": self.source,
                "source_activity_id": sa.source_activity_id,
            }

        wellness_records = [
            WellnessDaily(
                athlete_id=account.athlete_id,
                date=date(2024, 1, 1),
                source=self.source,
                resting_hr=50,
                hrv_ms=45.0,
                data_quality="authoritative",
                provenance={"source": self.source},
            ),
            WellnessDaily(
                athlete_id=account.athlete_id,
                date=date(2024, 1, 2),
                source=self.source,
                resting_hr=49,
                hrv_ms=46.5,
                data_quality="authoritative",
                provenance={"source": self.source},
            ),
        ]

        return ImportResult(
            source=self.source,
            start=date(2024, 1, 1),
            end=date(2024, 1, 7),
            source_activities=source_activities,
            activities=activities,
            wellness_records=wellness_records,
            capabilities=self.discover_capabilities(account),
        )

    def incremental_sync(self, account: AthleteSourceAccount) -> ImportResult:
        """Return an empty but well-formed sync result for the mock provider."""
        return ImportResult(source=self.source, start=None, end=None)

    def download_activity_file(
        self, account: AthleteSourceAccount, source_activity_id: str
    ) -> bytes | None:
        """Return a synthetic raw file payload."""
        return b"mock-fit-payload"

    def get_wellness(
        self, account: AthleteSourceAccount, start: date, end: date
    ) -> list[WellnessDaily]:
        """Return deterministic wellness records for the requested range."""
        records = []
        for day in range((end - start).days + 1):
            current = start + timedelta(days=day)
            records.append(
                WellnessDaily(
                    athlete_id=account.athlete_id,
                    date=current,
                    source=self.source,
                    resting_hr=50,
                    hrv_ms=45.0,
                    data_quality="authoritative",
                    provenance={"source": self.source},
                )
            )
        return records
