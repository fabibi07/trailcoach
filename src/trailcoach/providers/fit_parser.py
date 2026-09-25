"""Parse FIT files into canonical records and stream parquet files.

The parser is split in two stages so source providers can stay free of
database side effects:

1. `FitParser.parse_bytes` decodes a FIT payload into a `FitDocument`
   (pure, in-memory).
2. `FitParser.build_source_activity` / `build_activity` / `build_intervals`
   turn a `FitDocument` into model instances, and `persist_streams`
   writes the time-series parquet once an `Activity` id is known.

`FitParser.parse_to_source_activity` composes both stages and is kept for
the legacy `fitfile` ingestion path.
"""

import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import fitdecode  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from trailcoach.db.models import (
    Activity,
    ActivityInterval,
    ActivityStreamChannel,
    ActivityStreamSet,
    RawFile,
    SourceActivity,
)
from trailcoach.ingest.raw_store import RawStore
from trailcoach.ingest.streams import STREAM_COLUMNS, write_stream_parquet


def semicircles_to_degrees(v):
    if v is None:
        return None
    return v * (180.0 / (2 ** 31))


def none_or_float(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and v != v) else float(v)  # NaN check


# FIT field -> canonical channel name and unit
FIT_FIELD_MAP = {
    "timestamp": (
        "timestamp_s",
        None,
        lambda v: v.timestamp() if isinstance(v, datetime) else v,
    ),
    "position_lat": ("lat", "deg", semicircles_to_degrees),
    "position_long": ("lon", "deg", semicircles_to_degrees),
    "distance": ("distance_m", "m", none_or_float),
    "enhanced_altitude": ("altitude_m", "m", none_or_float),
    "altitude": ("altitude_m_legacy", "m", none_or_float),
    "enhanced_speed": ("speed_mps", "m/s", none_or_float),
    "speed": ("speed_mps_legacy", "m/s", none_or_float),
    "heart_rate": ("hr_bpm", "bpm", none_or_float),
    "cadence": ("cadence_spm", "spm", none_or_float),
    "power": ("power_w", "W", none_or_float),
    "temperature": ("temp_c", "C", none_or_float),
    "vertical_oscillation": ("vertical_oscillation_mm", "mm", none_or_float),
    "stance_time": ("stance_time_ms", "ms", none_or_float),
    "stance_time_balance": ("stance_time_balance_pct", "%", none_or_float),
    "step_length": ("step_length_mm", "mm", none_or_float),
    "vertical_ratio": ("vertical_ratio_pct", "%", none_or_float),
    "grade": ("grade_pct", "%", none_or_float),
}


def _first(fields: list[str], data: dict) -> Any:
    for f in fields:
        if f in data and data[f] is not None:
            return data[f]
    return None


def _sport_from_fit(sport_raw: str | None, sub_sport_raw: str | None) -> tuple[str, str | None]:
    s = (sport_raw or "").lower()
    ss = (sub_sport_raw or "").lower()
    if "trail" in ss or "trail" in s:
        return "trail_running", None
    if s in ("running", "run"):
        return "running", ss or None
    if s in ("cycling", "bike"):
        return "cycling", ss or None
    if s == "swimming":
        return "swimming", ss or None
    return s or "other", ss or None


def _parse_timestamp(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
    return None


@dataclass
class FitDeviceInfo:
    """Device identity as declared by the FIT `file_id`/`device_info` messages."""

    manufacturer: str | None = None
    product: str | None = None
    serial_number: str | None = None

    @property
    def name(self) -> str | None:
        parts = [p for p in [self.manufacturer, self.product] if p]
        return " ".join(str(p) for p in parts) if parts else None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "manufacturer": self.manufacturer,
            "model": self.product,
            "serial_number": self.serial_number,
        }


@dataclass
class FitDocument:
    """Decoded, still source-specific representation of one FIT file."""

    sha256: str
    file_id: dict[str, Any]
    device: FitDeviceInfo
    sport_raw: str | None
    sub_sport_raw: str | None
    sport: str
    sub_sport: str | None
    time_created: datetime | None
    start_time: datetime | None
    summary: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    laps: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_elapsed_s(self) -> float | None:
        return none_or_float(_first(["total_elapsed_time", "total_timer_time"], self.summary))

    @property
    def duration_moving_s(self) -> float | None:
        return none_or_float(_first(["total_moving_time", "total_timer_time"], self.summary))

    @property
    def distance_m(self) -> float | None:
        return none_or_float(_first(["total_distance"], self.summary))

    @property
    def ascent_m(self) -> float | None:
        return none_or_float(_first(["total_ascent"], self.summary))

    @property
    def descent_m(self) -> float | None:
        return none_or_float(_first(["total_descent"], self.summary))

    @property
    def avg_hr(self) -> float | None:
        return none_or_float(_first(["avg_heart_rate"], self.summary))

    @property
    def max_hr(self) -> float | None:
        return none_or_float(_first(["max_heart_rate"], self.summary))

    @property
    def avg_cadence(self) -> float | None:
        return none_or_float(_first(["avg_cadence", "avg_running_cadence"], self.summary))

    @property
    def avg_power(self) -> float | None:
        return none_or_float(_first(["avg_power"], self.summary))

    @property
    def end_time(self) -> datetime | None:
        if self.start_time is None or self.duration_elapsed_s is None:
            return None
        return self.start_time + timedelta(seconds=self.duration_elapsed_s)

    def natural_id(self) -> str | None:
        """Stable id from `file_id` (serial + time_created) when both exist."""
        serial = self.device.serial_number
        if serial and self.time_created:
            return f"{serial}-{self.time_created.strftime('%Y%m%dT%H%M%SZ')}"
        return None

    def canonical_stream_rows(self) -> list[dict[str, Any]]:
        """Normalize FIT `record` messages into canonical channel rows."""
        t0 = self.start_time.timestamp() if self.start_time else None
        last_dist = 0.0
        rows: list[dict[str, Any]] = []
        for r in self.records:
            ts = _parse_timestamp(r.get("timestamp"))
            t_s = ts.timestamp() if ts else None
            distance = none_or_float(r.get("distance"))
            if distance is not None:
                last_dist = distance
            else:
                distance = last_dist
            rows.append(
                {
                    "t_s": t_s,
                    "elapsed_s": (t_s - t0) if t_s is not None and t0 is not None else None,
                    "distance_m": distance,
                    "lat": semicircles_to_degrees(r.get("position_lat")),
                    "lon": semicircles_to_degrees(r.get("position_long")),
                    # fitdecode already applies scale/offset from the FIT profile.
                    "altitude_m": none_or_float(_first(["enhanced_altitude", "altitude"], r)),
                    "speed_mps": none_or_float(_first(["enhanced_speed", "speed"], r)),
                    "hr_bpm": none_or_float(r.get("heart_rate")),
                    "cadence_spm": none_or_float(r.get("cadence")),
                    "power_w": none_or_float(r.get("power")),
                    "temp_c": none_or_float(r.get("temperature")),
                    "vertical_oscillation_mm": none_or_float(r.get("vertical_oscillation")),
                    "stance_time_ms": none_or_float(r.get("stance_time")),
                    "stance_time_balance_pct": none_or_float(r.get("stance_time_balance")),
                    "step_length_mm": none_or_float(r.get("step_length")),
                    "vertical_ratio_pct": none_or_float(r.get("vertical_ratio")),
                }
            )
        return rows

    def present_channels(self) -> set[str]:
        """Canonical channels with at least one non-null sample."""
        present: set[str] = set()
        for row in self.canonical_stream_rows():
            for col in STREAM_COLUMNS:
                if col not in ("t_s", "elapsed_s") and row.get(col) is not None:
                    present.add(col)
        return present


@dataclass
class ParsedFit:
    source: SourceActivity
    activity: Activity | None
    stream_set: ActivityStreamSet | None
    stream_channels: list[ActivityStreamChannel]
    intervals: list[ActivityInterval]
    raw_file_id: UUID | None
    parser_version: str


class FitParser:
    """Parse a .FIT file into the canonical model.

    Idempotent: safe to call multiple times for the same RawFile.
    """

    VERSION = "0.2"

    def __init__(self, raw_store: RawStore | None = None, source: str = "fitfile") -> None:
        self.raw_store = raw_store or RawStore()
        self.source = source

    # ------------------------------------------------------------------ pure

    def parse_bytes(self, content: bytes) -> FitDocument:
        """Decode a FIT payload without touching the database or filesystem."""
        records: list[dict] = []
        laps: list[dict] = []
        sessions: list[dict] = []
        file_id_msg: dict = {}
        device_info: dict = {}

        with fitdecode.FitReader(io.BytesIO(content)) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                msg = frame
                fields = {f.field.name: f.value for f in msg.fields if f.field is not None}
                if msg.name == "file_id":
                    file_id_msg = fields
                elif msg.name == "device_info":
                    if not device_info:
                        device_info = fields
                elif msg.name == "record":
                    records.append(fields)
                elif msg.name == "lap":
                    laps.append(fields)
                elif msg.name == "session":
                    sessions.append(fields)

        summary = sessions[0] if sessions else (laps[0] if laps else {})
        sport_raw = _first(["sport", "sub_sport"], summary) or _first(["sport"], file_id_msg)
        sub_sport_raw = _first(["sub_sport"], summary)
        sport, sub_sport = _sport_from_fit(sport_raw, sub_sport_raw)

        time_created = _parse_timestamp(_first(["time_created"], file_id_msg))
        start_time = _parse_timestamp(_first(["start_time"], summary))
        if start_time is None and records:
            start_time = _parse_timestamp(records[0].get("timestamp"))
        if start_time is None:
            start_time = time_created

        return FitDocument(
            sha256=hashlib.sha256(content).hexdigest(),
            file_id=file_id_msg,
            device=self._device_info(device_info, file_id_msg),
            sport_raw=str(sport_raw) if sport_raw is not None else None,
            sub_sport_raw=str(sub_sport_raw) if sub_sport_raw is not None else None,
            sport=sport,
            sub_sport=sub_sport,
            time_created=time_created,
            start_time=start_time,
            summary=summary,
            records=records,
            laps=laps,
            sessions=sessions,
        )

    @staticmethod
    def _device_info(device_info: dict, file_id_msg: dict) -> FitDeviceInfo:
        manufacturer = _first(["manufacturer"], device_info) or _first(
            ["manufacturer"], file_id_msg
        )
        product = (
            _first(["product_name", "garmin_product", "product"], device_info)
            or _first(["garmin_product", "product"], file_id_msg)
        )
        serial = _first(["serial_number"], device_info) or _first(
            ["serial_number"], file_id_msg
        )
        return FitDeviceInfo(
            manufacturer=str(manufacturer) if manufacturer is not None else None,
            product=str(product) if product is not None else None,
            serial_number=str(serial) if serial is not None else None,
        )

    # -------------------------------------------------------------- builders

    def build_source_activity(
        self,
        doc: FitDocument,
        athlete_id: UUID,
        source_activity_id: str,
        raw_file_id: UUID | None = None,
        account_id: UUID | None = None,
        device_id: UUID | None = None,
    ) -> SourceActivity:
        return SourceActivity(
            athlete_id=athlete_id,
            source=self.source,
            source_activity_id=source_activity_id,
            start_time_utc=doc.start_time,
            distance_m=doc.distance_m,
            duration_elapsed_s=doc.duration_elapsed_s,
            duration_moving_s=doc.duration_moving_s,
            ascent_m=doc.ascent_m,
            sport_raw=doc.sport_raw or doc.sport,
            device_name=doc.device.name,
            external_id_raw=(
                json.dumps(doc.file_id, default=str)[:128] if doc.file_id else None
            ),
            summary_raw_file_id=raw_file_id,
            fit_raw_file_id=raw_file_id,
            account_id=account_id,
            device_id=device_id,
            status="normalized",
            data_quality="authoritative",
            quality_flags=[],
            provenance={
                "source": self.source,
                "source_record_id": source_activity_id,
                "raw_sha256": doc.sha256,
                "parser_version": self.VERSION,
                "device": doc.device.to_dict(),
            },
        )

    def build_activity(
        self,
        doc: FitDocument,
        athlete_id: UUID,
        provenance: dict[str, Any] | None = None,
    ) -> Activity:
        if doc.start_time is None:
            raise ValueError("FIT file has no start time")
        return Activity(
            athlete_id=athlete_id,
            start_time_utc=doc.start_time,
            start_time_local=doc.start_time,
            sport=doc.sport,
            sub_sport=doc.sub_sport,
            primary_source=self.source,
            data_quality="authoritative",
            quality_flags=[],
            duration_elapsed_s=doc.duration_elapsed_s,
            duration_moving_s=doc.duration_moving_s,
            distance_m=doc.distance_m,
            ascent_m=doc.ascent_m,
            descent_m=doc.descent_m,
            avg_hr=doc.avg_hr,
            max_hr=doc.max_hr,
            avg_cadence=doc.avg_cadence,
            avg_power=doc.avg_power,
            provenance=provenance
            or {
                "source": self.source,
                "raw_sha256": doc.sha256,
                "parser_version": self.VERSION,
            },
            name=None,
        )

    def build_intervals(
        self, activity_id: UUID | None, doc: FitDocument
    ) -> list[ActivityInterval]:
        intervals: list[ActivityInterval] = []
        for seq, lap in enumerate(doc.laps, start=1):
            start = _parse_timestamp(lap.get("start_time"))
            elapsed = none_or_float(_first(["total_elapsed_time", "total_timer_time"], lap))
            start_s = start.timestamp() if start else None
            end_s = (start_s + elapsed) if start_s and elapsed else None
            intervals.append(
                ActivityInterval(
                    activity_id=activity_id,
                    kind="device_lap",
                    seq=seq,
                    start_s=start_s or 0.0,
                    end_s=end_s or 0.0,
                    start_distance_m=none_or_float(lap.get("start_distance")),
                    end_distance_m=none_or_float(lap.get("total_distance")),
                    duration_s=elapsed,
                    distance_m=none_or_float(lap.get("total_distance")),
                    ascent_m=none_or_float(lap.get("total_ascent")),
                    descent_m=none_or_float(lap.get("total_descent")),
                    avg_hr=none_or_float(lap.get("avg_heart_rate")),
                    max_hr=none_or_float(lap.get("max_heart_rate")),
                    avg_speed_mps=none_or_float(lap.get("avg_speed")),
                    avg_power_w=none_or_float(lap.get("avg_power")),
                    avg_cadence=none_or_float(lap.get("avg_cadence")),
                    avg_grade=None,
                    vam_mph=None,
                    detector_version=self.VERSION,
                    meta={"lap_index": seq},
                )
            )
        return intervals

    def persist_streams(
        self,
        db: Session,
        activity_id: UUID,
        source_activity_id: UUID,
        doc: FitDocument,
        sha256: str,
    ) -> tuple[ActivityStreamSet | None, list[ActivityStreamChannel]]:
        return write_stream_parquet(
            db,
            activity_id=activity_id,
            source_activity_id=source_activity_id,
            rows=doc.canonical_stream_rows(),
            source=self.source,
            sha256=sha256,
            version=self.VERSION,
        )

    # ---------------------------------------------------------------- legacy

    def parse_to_source_activity(
        self, db: Session, raw_file: RawFile, athlete_id: UUID
    ) -> ParsedFit:
        content = self.raw_store.read_bytes(raw_file)
        source_activity_id = hashlib.sha256(content).hexdigest()

        existing = (
            db.query(SourceActivity)
            .filter(
                SourceActivity.source == self.source,
                SourceActivity.source_activity_id == source_activity_id,
            )
            .first()
        )
        if existing:
            return self._build_return_from_existing(db, existing)

        doc = self.parse_bytes(content)
        raw_file.parse_status = "ok"
        raw_file.parser_version = self.VERSION

        source_activity = self.build_source_activity(
            doc, athlete_id, source_activity_id, raw_file_id=raw_file.id
        )
        db.add(source_activity)
        db.flush()

        activity = self.build_activity(doc, athlete_id)
        db.add(activity)
        db.flush()
        source_activity.canonical_activity_id = activity.id

        stream_set, stream_channels = self.persist_streams(
            db, activity.id, source_activity.id, doc, raw_file.sha256
        )
        intervals = self.build_intervals(activity.id, doc)
        for iv in intervals:
            db.add(iv)

        db.flush()
        return ParsedFit(
            source=source_activity,
            activity=activity,
            stream_set=stream_set,
            stream_channels=stream_channels,
            intervals=intervals,
            raw_file_id=raw_file.id,
            parser_version=self.VERSION,
        )

    def _build_return_from_existing(self, db: Session, existing: SourceActivity) -> ParsedFit:
        activity = db.query(Activity).filter(Activity.id == existing.canonical_activity_id).first()
        if activity is None:
            raise RuntimeError("Orphaned source_activity without canonical activity")
        stream_set = (
            db.query(ActivityStreamSet)
            .filter(ActivityStreamSet.source_activity_id == existing.id)
            .first()
        )
        stream_channels = (
            db.query(ActivityStreamChannel)
            .filter(ActivityStreamChannel.stream_set_id == stream_set.id)
            .all()
            if stream_set
            else []
        )
        intervals = (
            db.query(ActivityInterval).filter(ActivityInterval.activity_id == activity.id).all()
        )
        return ParsedFit(
            source=existing,
            activity=activity,
            stream_set=stream_set,
            stream_channels=stream_channels,
            intervals=intervals,
            raw_file_id=existing.fit_raw_file_id,
            parser_version=self.VERSION,
        )
