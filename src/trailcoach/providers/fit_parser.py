"""Parse FIT files into canonical records and stream parquet files."""

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import fitdecode  # type: ignore[import-untyped]
import polars as pl
from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import (
    Activity,
    ActivityInterval,
    ActivityStreamChannel,
    ActivityStreamSet,
    RawFile,
    SourceActivity,
)
from trailcoach.ingest.raw_store import RawStore


def semicircles_to_degrees(v):
    if v is None:
        return None
    return v * (180.0 / (2 ** 31))


def none_or_float(v):
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


def _try_get(msg, field_name: str, default=None, cast=None):
    try:
        v = msg.get_value(field_name)
    except Exception:
        return default
    if v is None:
        return default
    if v == "None":  # Sometimes fitdecode returns "None"
        return default
    if cast:
        try:
            return cast(v)
        except Exception:
            return default
    return v


@dataclass
class ParsedFit:
    source: SourceActivity
    activity: Activity | None
    stream_set: ActivityStreamSet | None
    stream_channels: list[ActivityStreamChannel]
    intervals: list[ActivityInterval]
    raw_file_id: UUID | None
    parser_version: str


def _parse_timestamp(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
    return None


def _format_time_created(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


class FitParser:
    """Parse a .FIT file into the canonical model.

    Idempotent: safe to call multiple times for the same RawFile.
    """

    VERSION = "0.1"

    def __init__(self, raw_store: RawStore | None = None) -> None:
        self.raw_store = raw_store or RawStore()

    def parse_to_source_activity(
        self, db: Session, raw_file: RawFile, athlete_id: UUID
    ) -> ParsedFit:
        content = self.raw_store.read_bytes(raw_file)
        path = raw_file.storage_path
        source_activity_id = self._derive_source_activity_id(content, path)

        # Check if already parsed
        existing = (
            db.query(SourceActivity)
            .filter(
                SourceActivity.source == "fitfile",
                SourceActivity.source_activity_id == source_activity_id,
            )
            .first()
        )
        if existing:
            # Re-parse fresh into a temp object without committing?
            # For idempotency, return existing. To support reprocessing,
            # we would delete existing source rows first.
            return self._build_return_from_existing(db, existing)

        raw_file.parse_status = "ok"
        raw_file.parser_version = self.VERSION

        # Parse file
        records: list[dict] = []
        laps: list[dict] = []
        sessions: list[dict] = []
        file_id_msg: dict = {}
        device_info: dict = {}
        events: list[dict] = []

        with fitdecode.FitReader(io.BytesIO(content)) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                msg = frame
                name = msg.name
                fields = {f.field.name: f.value for f in msg.fields if f.field is not None}
                if name == "file_id":
                    file_id_msg = fields
                elif name == "device_info":
                    device_info = fields
                elif name == "record":
                    records.append(fields)
                elif name == "lap":
                    laps.append(fields)
                elif name == "session":
                    sessions.append(fields)
                elif name == "event":
                    events.append(fields)

        # Sport / time
        sport_raw = _first(
            ["sport", "sub_sport"],
            sessions[0] if sessions else (laps[0] if laps else {}),
        ) or _first(["sport"], file_id_msg)
        sub_sport_raw = _first(
            ["sub_sport"], sessions[0] if sessions else (laps[0] if laps else {})
        )
        sport, sub_sport = _sport_from_fit(sport_raw, sub_sport_raw)

        time_created = _parse_timestamp(_first(["time_created"], file_id_msg))
        start_time = _parse_timestamp(
            _first(["start_time"], sessions[0] if sessions else (laps[0] if laps else {}))
        )
        if start_time is None and records:
            start_time = _parse_timestamp(records[0].get("timestamp"))
        if start_time is None:
            start_time = _parse_timestamp(time_created)

        device_name = self._device_name(device_info, file_id_msg)

        # Summary from session (preferred) or lap
        summary = sessions[0] if sessions else (laps[0] if laps else {})
        duration_elapsed = none_or_float(
            _first(["total_elapsed_time", "total_timer_time"], summary)
        )
        duration_moving = none_or_float(_first(["total_moving_time", "total_timer_time"], summary))
        distance_m = none_or_float(_first(["total_distance"], summary))
        ascent_m = none_or_float(_first(["total_ascent"], summary))
        descent_m = none_or_float(_first(["total_descent"], summary))
        avg_hr = none_or_float(_first(["avg_heart_rate"], summary))
        max_hr = none_or_float(_first(["max_heart_rate"], summary))
        avg_cadence = none_or_float(_first(["avg_cadence"], summary))
        avg_power = none_or_float(_first(["avg_power"], summary))

        source_activity = SourceActivity(
            athlete_id=athlete_id,
            source="fitfile",
            source_activity_id=source_activity_id,
            start_time_utc=start_time,
            distance_m=distance_m,
            duration_elapsed_s=duration_elapsed,
            duration_moving_s=duration_moving,
            ascent_m=ascent_m,
            sport_raw=sport_raw or sport,
            device_name=device_name,
            external_id_raw=json.dumps(file_id_msg, default=str)[:255] if file_id_msg else None,
            summary_raw_file_id=raw_file.id,
            fit_raw_file_id=raw_file.id,
            status="normalized",
            data_quality="authoritative",
            quality_flags=[],
        )
        db.add(source_activity)
        db.flush()

        activity = Activity(
            athlete_id=athlete_id,
            start_time_utc=start_time,
            start_time_local=start_time,
            sport=sport,
            sub_sport=sub_sport,
            primary_source="fitfile",
            data_quality="authoritative",
            quality_flags=[],
            duration_elapsed_s=duration_elapsed,
            duration_moving_s=duration_moving,
            distance_m=distance_m,
            ascent_m=ascent_m,
            descent_m=descent_m,
            avg_hr=avg_hr,
            max_hr=max_hr,
            avg_cadence=avg_cadence,
            avg_power=avg_power,
            provenance={
                "source": "fitfile",
                "raw_sha256": raw_file.sha256,
                "parser_version": self.VERSION,
            },
            name=None,
        )
        db.add(activity)
        db.flush()

        source_activity.canonical_activity_id = activity.id

        # Streams to parquet
        stream_set, stream_channels = self._write_stream_parquet(
            db, activity.id, source_activity.id, records, raw_file, start_time
        )

        # Intervals from laps
        intervals = self._build_intervals(activity.id, laps, sessions)
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

    def _derive_source_activity_id(self, content: bytes, path: str) -> str:
        # Use sha256 as the canonical id for fitfile source. The file_id
        # time_created + serial is better, but parsing it just for an id is
        # heavy; sha256 is unique and stable.
        return hashlib.sha256(content).hexdigest()

    def _device_name(self, device_info: dict, file_id_msg: dict) -> str | None:
        manufacturer = (
            _first(["product_name", "manufacturer"], device_info)
            or _first(["manufacturer"], file_id_msg)
        )
        product = _first(["product"], device_info) or _first(["product"], file_id_msg)
        parts = [p for p in [manufacturer, product] if p]
        return " ".join(str(p) for p in parts) if parts else None

    def _write_stream_parquet(
        self,
        db: Session,
        activity_id: UUID,
        source_activity_id: UUID,
        records: list[dict],
        raw_file: RawFile,
        start_time: datetime | None,
    ) -> tuple[ActivityStreamSet | None, list[ActivityStreamChannel]]:
        if not records:
            return None, []

        rows: dict[str, list] = {
            "t_s": [],
            "elapsed_s": [],
            "distance_m": [],
            "lat": [],
            "lon": [],
            "altitude_m": [],
            "speed_mps": [],
            "hr_bpm": [],
            "cadence_spm": [],
            "power_w": [],
            "temp_c": [],
            "vertical_oscillation_mm": [],
            "stance_time_ms": [],
            "stance_time_balance_pct": [],
            "step_length_mm": [],
            "vertical_ratio_pct": [],
        }

        t0 = start_time.timestamp() if start_time else None
        last_dist = 0.0
        for r in records:
            ts = _parse_timestamp(r.get("timestamp"))
            t_s = ts.timestamp() if ts else None
            elapsed_s = (t_s - t0) if t_s is not None and t0 is not None else None

            lat = semicircles_to_degrees(r.get("position_lat"))
            lon = semicircles_to_degrees(r.get("position_long"))
            distance = none_or_float(r.get("distance"))
            if distance is not None:
                last_dist = distance
            else:
                distance = last_dist

            # fitdecode already applies scale/offset from the FIT profile,
            # so the values are already in their declared units.
            alt = none_or_float(_first(["enhanced_altitude", "altitude"], r))
            speed = none_or_float(_first(["enhanced_speed", "speed"], r))
            hr = none_or_float(r.get("heart_rate"))
            cad = none_or_float(r.get("cadence"))
            power = none_or_float(r.get("power"))
            temp = none_or_float(r.get("temperature"))
            vosc = none_or_float(r.get("vertical_oscillation"))
            stance = none_or_float(r.get("stance_time"))
            stance_bal = none_or_float(r.get("stance_time_balance"))
            step_len = none_or_float(r.get("step_length"))
            vrat = none_or_float(r.get("vertical_ratio"))

            rows["t_s"].append(t_s)
            rows["elapsed_s"].append(elapsed_s)
            rows["distance_m"].append(distance)
            rows["lat"].append(lat)
            rows["lon"].append(lon)
            rows["altitude_m"].append(alt)
            rows["speed_mps"].append(speed)
            rows["hr_bpm"].append(hr)
            rows["cadence_spm"].append(cad)
            rows["power_w"].append(power)
            rows["temp_c"].append(temp)
            rows["vertical_oscillation_mm"].append(vosc)
            rows["stance_time_ms"].append(stance)
            rows["stance_time_balance_pct"].append(stance_bal)
            rows["step_length_mm"].append(step_len)
            rows["vertical_ratio_pct"].append(vrat)

        df = pl.DataFrame(rows)
        # Remove columns that are all null to keep schema flexible
        df = df[[c for c in df.columns if df[c].null_count() != df.height]]

        rel = Path(f"fitfile/streams/{activity_id}.parquet")
        path = settings.processed_root_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)

        # Compute channel stats
        channels = []
        for col in df.columns:
            if col == "t_s":
                continue
            series = df[col]
            n_missing = series.null_count()
            n_valid = df.height - n_missing
            if n_valid == 0:
                continue
            mean_val = series.mean()
            p50_val = series.quantile(0.5)
            p95_val = series.quantile(0.95)
            channel_name, unit, _ = FIT_FIELD_MAP.get(col, (col, None, None))
            ch = ActivityStreamChannel(
                stream_set_id=None,  # filled after stream_set creation
                channel=channel_name,
                unit=unit,
                n_valid=n_valid,
                n_missing=n_missing,
                min=series.min(),
                max=series.max(),
                mean=mean_val,
                p50=p50_val,
                p95=p95_val,
                is_derived=False,
                calc_version=self.VERSION,
            )
            channels.append(ch)

        # Estimate sample rate from t_s
        sample_rate = None
        if "t_s" in df.columns and df.height > 1:
            t = df["t_s"].drop_nulls().to_list()
            if len(t) >= 2:
                diffs = [t[i + 1] - t[i] for i in range(len(t) - 1) if t[i + 1] - t[i] > 0]
                if diffs:
                    sample_rate = 1.0 / (sum(diffs) / len(diffs))

        stream_set = ActivityStreamSet(
            activity_id=activity_id,
            source_activity_id=source_activity_id,
            storage_path=str(rel),
            sample_count=df.height,
            sample_rate_hz=sample_rate,
            series_type="time",
            channels=[ch.channel for ch in channels],
            parser_version=self.VERSION,
            sha256=raw_file.sha256,
            gap_summary={},
        )
        db.add(stream_set)
        db.flush()
        for ch in channels:
            ch.stream_set_id = stream_set.id
            db.add(ch)
        return stream_set, channels

    def _build_intervals(
        self, activity_id: UUID, laps: list[dict], sessions: list[dict]
    ) -> list[ActivityInterval]:
        intervals: list[ActivityInterval] = []
        for seq, lap in enumerate(laps, start=1):
            start = _parse_timestamp(lap.get("start_time"))
            elapsed = none_or_float(_first(["total_elapsed_time", "total_timer_time"], lap))
            start_s = (
                (start - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
                if start
                else None
            )
            end_s = (start_s + elapsed) if start_s and elapsed else None
            iv = ActivityInterval(
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
            intervals.append(iv)
        return intervals

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
            if activity
            else []
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
