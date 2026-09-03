import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trailcoach.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Athlete(Base):
    __tablename__ = "athlete"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str | None] = mapped_column(String(120))
    birth_date: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(String(1))
    tz_default: Mapped[str] = mapped_column(String(64), default="UTC")
    units: Mapped[str] = mapped_column(String(16), default="metric")


class AthleteThreshold(Base):
    __tablename__ = "athlete_threshold"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), nullable=False
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("athlete_id", "kind", "valid_from"),)


class AthleteZone(Base):
    __tablename__ = "athlete_zone"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    zone_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lower: Mapped[float | None] = mapped_column(Numeric(12, 6))
    upper: Mapped[float | None] = mapped_column(Numeric(12, 6))
    label: Mapped[str | None] = mapped_column(String(64))


class RawFile(Base):
    __tablename__ = "raw_file"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_activity_id: Mapped[str | None] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(64))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    parser_version: Mapped[str | None] = mapped_column(String(16))
    parse_error: Mapped[str | None] = mapped_column(Text)


class EventInbox(Base):
    __tablename__ = "event_inbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    subscription_id: Mapped[str | None] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(32))
    aspect_type: Mapped[str | None] = mapped_column(String(16))
    object_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="received")


class Activity(Base):
    __tablename__ = "activity"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), nullable=False
    )
    start_time_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    start_time_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tz: Mapped[str | None] = mapped_column(String(64))
    sport: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sub_sport: Mapped[str | None] = mapped_column(String(32), index=True)
    primary_source: Mapped[str] = mapped_column(String(32), nullable=False)
    data_quality: Mapped[str] = mapped_column(String(16), default="authoritative")
    quality_flags: Mapped[list[str] | None] = mapped_column(JSON, default=list)

    duration_elapsed_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_moving_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    ascent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    descent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    avg_hr: Mapped[float | None] = mapped_column(Numeric(6, 2))
    max_hr: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_cadence: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_power: Mapped[float | None] = mapped_column(Numeric(8, 2))

    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    is_race: Mapped[bool] = mapped_column(Boolean, default=False)
    rpe: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)


class SourceActivity(Base):
    __tablename__ = "source_activity"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="SET NULL"), nullable=True
    )
    start_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_elapsed_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_moving_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    ascent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    sport_raw: Mapped[str | None] = mapped_column(String(32))
    device_name: Mapped[str | None] = mapped_column(String(128))
    external_id_raw: Mapped[str | None] = mapped_column(String(128))
    summary_raw_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_file.id", ondelete="SET NULL"), nullable=True
    )
    fit_raw_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_file.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="new")
    data_quality: Mapped[str] = mapped_column(String(16), default="degraded")
    quality_flags: Mapped[list[str] | None] = mapped_column(JSON, default=list)

    __table_args__ = (UniqueConstraint("source", "source_activity_id"),)


class ActivityLink(Base):
    __tablename__ = "activity_link"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="CASCADE"), nullable=False
    )
    source_activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_activity.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.0)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActivityStreamSet(Base):
    __tablename__ = "activity_stream_set"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="CASCADE"), nullable=False
    )
    source_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("source_activity.id", ondelete="SET NULL"), nullable=True
    )
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_rate_hz: Mapped[float | None] = mapped_column(Numeric(8, 4))
    series_type: Mapped[str] = mapped_column(String(16), default="time")
    channels: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    parser_version: Mapped[str | None] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    gap_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (UniqueConstraint("source_activity_id"),)


class ActivityStreamChannel(Base):
    __tablename__ = "activity_stream_channel"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stream_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity_stream_set.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(16))
    n_valid: Mapped[int] = mapped_column(Integer, default=0)
    n_missing: Mapped[int] = mapped_column(Integer, default=0)
    min: Mapped[float | None] = mapped_column(Numeric(16, 6))
    max: Mapped[float | None] = mapped_column(Numeric(16, 6))
    mean: Mapped[float | None] = mapped_column(Numeric(16, 6))
    p50: Mapped[float | None] = mapped_column(Numeric(16, 6))
    p95: Mapped[float | None] = mapped_column(Numeric(16, 6))
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    calc_version: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (UniqueConstraint("stream_set_id", "channel"),)


class ActivityInterval(Base):
    __tablename__ = "activity_interval"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    start_s: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    end_s: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    start_distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    end_distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    duration_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    ascent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    descent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    avg_hr: Mapped[float | None] = mapped_column(Numeric(6, 2))
    max_hr: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_speed_mps: Mapped[float | None] = mapped_column(Numeric(10, 4))
    avg_gap_speed_mps: Mapped[float | None] = mapped_column(Numeric(10, 4))
    avg_power_w: Mapped[float | None] = mapped_column(Numeric(8, 2))
    avg_cadence: Mapped[float | None] = mapped_column(Numeric(6, 2))
    avg_grade: Mapped[float | None] = mapped_column(Numeric(8, 4))
    vam_mph: Mapped[float | None] = mapped_column(Numeric(10, 2))
    detector_version: Mapped[str | None] = mapped_column(String(16))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ActivityFeature(Base):
    __tablename__ = "activity_feature"

    activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="CASCADE"), primary_key=True
    )
    feature_set_version: Mapped[str] = mapped_column(String(16), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    inputs_hash: Mapped[str | None] = mapped_column(String(64))

    gap_avg_speed_mps: Mapped[float | None] = mapped_column(Numeric(10, 6))
    ngp_mps: Mapped[float | None] = mapped_column(Numeric(10, 6))
    intensity_factor: Mapped[float | None] = mapped_column(Numeric(6, 4))
    ascent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    descent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    vertical_work_j_per_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    climbing_rate_mph: Mapped[float | None] = mapped_column(Numeric(10, 2))
    descent_rate_mph: Mapped[float | None] = mapped_column(Numeric(10, 2))
    technical_descent_pct: Mapped[float | None] = mapped_column(Numeric(6, 3))
    hr_drift_pct: Mapped[float | None] = mapped_column(Numeric(6, 3))
    decoupling_pct: Mapped[float | None] = mapped_column(Numeric(6, 3))
    efficiency_index: Mapped[float | None] = mapped_column(Numeric(8, 4))
    time_in_hr_zone: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    time_in_pace_zone: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    time_in_grade_bin: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    best_efforts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    mean_max_curve_path: Mapped[str | None] = mapped_column(String(512))
    running_dynamics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ActivityLoad(Base):
    __tablename__ = "activity_load"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("activity.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_reason: Mapped[str | None] = mapped_column(String(255))
    threshold_refs: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(16), nullable=False)
    inputs_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("activity_id", "method", "model_version"),)


class DailyLoad(Base):
    __tablename__ = "daily_load"

    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    load_primary: Mapped[float | None] = mapped_column(Numeric(12, 3))
    load_by_method: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    duration_s: Mapped[float | None] = mapped_column(Numeric(12, 3))
    distance_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    ascent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    descent_m: Mapped[float | None] = mapped_column(Numeric(12, 3))
    vertical_work_j_per_kg: Mapped[float | None] = mapped_column(Numeric(12, 3))
    n_activities: Mapped[int] = mapped_column(Integer, default=0)
    has_degraded: Mapped[bool] = mapped_column(Boolean, default=False)


class TrainingMetricDaily(Base):
    __tablename__ = "training_metric_daily"

    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    ctl: Mapped[float | None] = mapped_column(Numeric(10, 3))
    atl: Mapped[float | None] = mapped_column(Numeric(10, 3))
    tsb: Mapped[float | None] = mapped_column(Numeric(10, 3))
    ramp_rate_7d: Mapped[float | None] = mapped_column(Numeric(8, 4))
    monotony_7d: Mapped[float | None] = mapped_column(Numeric(8, 4))
    strain_7d: Mapped[float | None] = mapped_column(Numeric(10, 3))
    ctl_ascent: Mapped[float | None] = mapped_column(Numeric(10, 3))
    atl_ascent: Mapped[float | None] = mapped_column(Numeric(10, 3))
    model_version: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)


class WellnessDaily(Base):
    __tablename__ = "wellness_daily"

    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str | None] = mapped_column(String(32))
    resting_hr: Mapped[float | None] = mapped_column(Numeric(6, 2))
    hrv_status: Mapped[str | None] = mapped_column(String(16))
    hrv_ms: Mapped[float | None] = mapped_column(Numeric(8, 4))
    body_battery_min: Mapped[int | None] = mapped_column(SmallInteger)
    body_battery_max: Mapped[int | None] = mapped_column(SmallInteger)
    sleep_score: Mapped[int | None] = mapped_column(SmallInteger)
    sleep_duration_s: Mapped[float | None] = mapped_column(Numeric(10, 3))
    training_readiness: Mapped[int | None] = mapped_column(SmallInteger)
    vo2max: Mapped[float | None] = mapped_column(Numeric(6, 2))
    weight_kg: Mapped[float | None] = mapped_column(Numeric(6, 3))
    raw_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_file.id", ondelete="SET NULL"), nullable=True
    )


class AthleteStateSnapshot(Base):
    __tablename__ = "athlete_state_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    athlete_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("athlete.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    state_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("athlete_id", "as_of", "state_version"),)


class Job(Base):
    __tablename__ = "job"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceState(Base):
    __tablename__ = "source_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    auth_status: Mapped[str] = mapped_column(String(32), default="unknown")
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class CalcVersionRegistry(Base):
    __tablename__ = "calc_version_registry"

    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)


class DedupeReview(Base):
    __tablename__ = "dedupe_review"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_activity.id", ondelete="CASCADE"), nullable=False
    )
    candidates: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
