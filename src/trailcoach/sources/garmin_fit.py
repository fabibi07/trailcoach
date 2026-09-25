"""Garmin FIT file provider.

First real `SourceProvider`. It reads activity `.fit` files exported from a
Garmin device or Garmin Connect from a local directory. There is no network
access, no OAuth and no `python-garminconnect`: the "credential" of this
source is simply the import directory, stored in
`AthleteSourceAccount.auth_json["import_dir"]`.

The provider is DB-free: it parses files into detached model instances and
`ActivityPayload`s; `IngestionOrchestrator` handles raw storage, dedup,
device registration and persistence.
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any

from trailcoach.db.models import (
    AthleteSourceAccount,
    MetricCapability,
    WellnessDaily,
)
from trailcoach.providers.fit_parser import FitDocument, FitParser
from trailcoach.sources.provider import (
    ActivityPayload,
    HistoricalRange,
    ImportResult,
    SourceProvider,
)
from trailcoach.sources.registry import SourceProviderRegistry

GARMIN_FIT_CAPABILITIES: list[tuple[str, str, str | None]] = [
    ("heart_rate", "native", "Optical or chest strap HR recorded in FIT records"),
    ("gps", "native", "position_lat/position_long in FIT records"),
    ("distance", "native", None),
    ("duration", "native", None),
    ("speed", "native", None),
    ("pace", "derived", "Derived from speed/distance"),
    ("elevation_gain", "native", "Barometric or GPS altitude depending on device"),
    ("cadence", "native", None),
    ("power", "native", "Only when a power sensor / running power is recorded"),
    ("temperature", "native", "Only on devices with a temperature sensor"),
    ("running_dynamics", "native", "Only with compatible sensor"),
    ("hrv", "unavailable", "Daily HRV is not part of activity FIT files"),
    ("resting_hr", "unavailable", "Requires Garmin Connect wellness export"),
    ("sleep_score", "unavailable", "Requires Garmin Connect wellness export"),
    ("body_battery", "unavailable", "Requires Garmin Connect wellness export"),
]

CURSOR_KEY = "last_start_time_utc"


@SourceProviderRegistry.register
class GarminFitProvider(SourceProvider):
    source = "garmin_fit"

    def __init__(self, import_dir: Path | str | None = None, extension: str = ".fit") -> None:
        self.import_dir = Path(import_dir) if import_dir is not None else None
        self.extension = extension
        self.parser = FitParser(source=self.source)

    # ------------------------------------------------------------ helpers

    def _resolve_dir(self, account: AthleteSourceAccount) -> Path | None:
        if self.import_dir is not None:
            return self.import_dir
        auth = account.auth_json or {}
        raw = auth.get("import_dir")
        return Path(raw).expanduser() if raw else None

    def _scan(
        self, account: AthleteSourceAccount
    ) -> tuple[list[tuple[Path, bytes, FitDocument]], list[str]]:
        """Parse every FIT file under the import dir; corrupt files become errors."""
        directory = self._resolve_dir(account)
        if directory is None or not directory.is_dir():
            return [], []
        docs: list[tuple[Path, bytes, FitDocument]] = []
        errors: list[str] = []
        for path in sorted(directory.rglob(f"*{self.extension}")):
            content = path.read_bytes()
            try:
                docs.append((path, content, self.parser.parse_bytes(content)))
            except Exception as exc:  # noqa: BLE001 - surface per-file parse failures
                errors.append(f"{path.name}: {exc}")
        docs.sort(key=lambda item: item[2].start_time.isoformat() if item[2].start_time else "")
        return docs, errors

    @staticmethod
    def source_activity_id(doc: FitDocument) -> str:
        """Prefer the Garmin `file_id` identity; fall back to content hash."""
        return doc.natural_id() or doc.sha256

    def _build_result(
        self,
        account: AthleteSourceAccount,
        items: list[tuple[Path, bytes, FitDocument]],
        start: date | None,
        end: date | None,
        errors: list[str],
    ) -> ImportResult:
        result = ImportResult(source=self.source, start=start, end=end, errors=list(errors))
        result.capabilities = self.discover_capabilities(account)
        last_start: datetime | None = None

        for path, content, doc in items:
            sa_id = self.source_activity_id(doc)
            if doc.start_time is None:
                result.errors.append(f"{path.name}: no start time; skipped")
                continue
            sa = self.parser.build_source_activity(
                doc, account.athlete_id, sa_id, account_id=account.id
            )
            activity = self.parser.build_activity(doc, account.athlete_id)
            result.source_activities.append(sa)
            result.activities.append(activity)
            result.payloads[sa_id] = ActivityPayload(
                source_activity_id=sa_id,
                raw_content=content,
                raw_kind="fit",
                raw_extension=self.extension,
                raw_content_type="application/vnd.ant.fit",
                stream_rows=doc.canonical_stream_rows(),
                intervals=self.parser.build_intervals(None, doc),
                device=doc.device.to_dict(),
            )
            if last_start is None or doc.start_time > last_start:
                last_start = doc.start_time

        if last_start is not None:
            result.cursor = {CURSOR_KEY: last_start.isoformat()}
        return result

    # ----------------------------------------------------------- contract

    def authenticate(self, account: AthleteSourceAccount) -> dict[str, Any]:
        directory = self._resolve_dir(account)
        if directory is None:
            return {
                "status": "error",
                "auth_status": "missing_import_dir",
                "error": "auth_json.import_dir is not configured",
            }
        if not directory.is_dir():
            return {
                "status": "error",
                "auth_status": "import_dir_not_found",
                "error": f"{directory} is not a directory",
            }
        count = sum(1 for _ in directory.rglob(f"*{self.extension}"))
        return {
            "status": "ok",
            "auth_status": "not_required",
            "source_type": "file_export",
            "import_dir": str(directory),
            "file_count": count,
        }

    def discover_capabilities(self, account: AthleteSourceAccount) -> list[MetricCapability]:
        return [
            MetricCapability(
                source=self.source,
                metric=metric,
                availability=availability,
                confidence_base=0.95 if availability == "native" else None,
                notes=notes,
            )
            for metric, availability, notes in GARMIN_FIT_CAPABILITIES
        ]

    def get_historical_range(self, account: AthleteSourceAccount) -> HistoricalRange:
        docs, _ = self._scan(account)
        starts = [doc.start_time for _, _, doc in docs if doc.start_time]
        if not starts:
            return HistoricalRange()
        return HistoricalRange(start=min(starts).date(), end=max(starts).date())

    def initial_import(
        self,
        account: AthleteSourceAccount,
        start: date | None = None,
        end: date | None = None,
    ) -> ImportResult:
        docs, errors = self._scan(account)
        items = [
            item
            for item in docs
            if item[2].start_time is None
            or (
                (start is None or item[2].start_time.date() >= start)
                and (end is None or item[2].start_time.date() <= end)
            )
        ]
        return self._build_result(account, items, start, end, errors)

    def incremental_sync(self, account: AthleteSourceAccount) -> ImportResult:
        cursor = (account.cursor_json or {}).get(CURSOR_KEY)
        since = datetime.fromisoformat(cursor) if cursor else None
        docs, errors = self._scan(account)
        items = [
            item
            for item in docs
            if since is None or item[2].start_time is None or item[2].start_time > since
        ]
        result = self._build_result(account, items, None, None, errors)
        if not result.cursor and cursor:
            result.cursor = {CURSOR_KEY: cursor}
        return result

    def download_activity_file(
        self, account: AthleteSourceAccount, source_activity_id: str
    ) -> bytes | None:
        docs, _ = self._scan(account)
        for _, content, doc in docs:
            if self.source_activity_id(doc) == source_activity_id:
                return content
        return None

    def get_wellness(
        self, account: AthleteSourceAccount, start: date, end: date
    ) -> list[WellnessDaily]:
        """Activity FIT files carry no daily wellness; see `unavailable` capabilities."""
        return []
