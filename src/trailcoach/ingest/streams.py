"""Source-agnostic persistence of activity time-series to parquet."""

from pathlib import Path
from typing import Any
from uuid import UUID

import polars as pl
from sqlalchemy.orm import Session

from trailcoach.core.config import settings
from trailcoach.db.models import ActivityStreamChannel, ActivityStreamSet

STREAM_COLUMNS: tuple[str, ...] = (
    "t_s",
    "elapsed_s",
    "distance_m",
    "lat",
    "lon",
    "altitude_m",
    "speed_mps",
    "hr_bpm",
    "cadence_spm",
    "power_w",
    "temp_c",
    "vertical_oscillation_mm",
    "stance_time_ms",
    "stance_time_balance_pct",
    "step_length_mm",
    "vertical_ratio_pct",
)


def write_stream_parquet(
    db: Session,
    activity_id: UUID,
    source_activity_id: UUID,
    rows: list[dict[str, Any]],
    source: str,
    sha256: str | None,
    version: str,
) -> tuple[ActivityStreamSet | None, list[ActivityStreamChannel]]:
    """Persist canonical channel rows as parquet and register the stream set.

    `rows` must already be normalized to canonical channel names (see
    `STREAM_COLUMNS`); this function knows nothing about FIT or any other
    source format.
    """
    if not rows:
        return None, []

    columns: dict[str, list[Any]] = {c: [] for c in STREAM_COLUMNS}
    for row in rows:
        for c in STREAM_COLUMNS:
            columns[c].append(row.get(c))

    df = pl.DataFrame(columns)
    df = df[[c for c in df.columns if df[c].null_count() != df.height]]

    rel = Path(f"{source}/streams/{activity_id}.parquet")
    path = settings.processed_root_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)

    unit_by_channel: dict[str, str] = {
        "lat": "deg",
        "lon": "deg",
        "distance_m": "m",
        "altitude_m": "m",
        "speed_mps": "m/s",
        "hr_bpm": "bpm",
        "cadence_spm": "spm",
        "power_w": "W",
        "temp_c": "C",
        "vertical_oscillation_mm": "mm",
        "stance_time_ms": "ms",
        "stance_time_balance_pct": "%",
        "step_length_mm": "mm",
        "vertical_ratio_pct": "%",
        "elapsed_s": "s",
    }

    channels: list[ActivityStreamChannel] = []
    for col in df.columns:
        if col == "t_s":
            continue
        series = df[col]
        n_missing = series.null_count()
        n_valid = df.height - n_missing
        if n_valid == 0:
            continue
        channels.append(
            ActivityStreamChannel(
                stream_set_id=None,
                channel=col,
                unit=unit_by_channel.get(col),
                n_valid=n_valid,
                n_missing=n_missing,
                min=series.min(),
                max=series.max(),
                mean=series.mean(),
                p50=series.quantile(0.5),
                p95=series.quantile(0.95),
                is_derived=False,
                calc_version=version,
            )
        )

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
        parser_version=version,
        sha256=sha256,
        gap_summary={},
    )
    db.add(stream_set)
    db.flush()
    for ch in channels:
        ch.stream_set_id = stream_set.id
        db.add(ch)
    return stream_set, channels
