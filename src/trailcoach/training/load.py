"""Per-activity training load calculations (rTSS, HRSS, TRIMP, TSS_power)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from trailcoach.db.models import Activity, Athlete, AthleteThreshold
from trailcoach.training.grade import add_grade_to_dataframe, grade_adjusted_speed

MODEL_VERSION = "0.1.0"
ROLLING_SECONDS = 30


def _hr_ratio(hr: float, hr_rest: float, hr_max: float) -> float:
    return (hr - hr_rest) / (hr_max - hr_rest)


def _trimp(duration_min: float, hr_ratio_value: float, sex: str | None) -> float:
    """Bannister TRIMP (men / women variants)."""
    if sex and sex.upper() == "F":
        return duration_min * hr_ratio_value * 0.86 * math.exp(1.67 * hr_ratio_value)
    return duration_min * hr_ratio_value * 0.64 * math.exp(1.92 * hr_ratio_value)


def _as_float(value: Any) -> float | None:
    """Coerce a polars scalar to float, returning None for non-numeric/null."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _np4(series: pl.Series) -> float | None:
    """4th root of the mean of the 4th powers (NP/NGP estimator)."""
    s = series.drop_nulls()
    if len(s) == 0:
        return None
    mean_fourth = _as_float((s**4).mean())
    if mean_fourth is None or mean_fourth <= 0:
        return None
    return float(mean_fourth**0.25)


def _intensity_factor(value: float | None, threshold: float | None) -> float | None:
    if value is None or threshold is None or threshold <= 0 or value < 0:
        return None
    return value / threshold


def _tss_from_if(duration_s: float, intensity_factor: float | None) -> float | None:
    if intensity_factor is None or duration_s <= 0:
        return None
    return duration_s * intensity_factor**2 / 36.0


def _activity_duration_s(activity: Activity) -> float:
    return float(activity.duration_moving_s or activity.duration_elapsed_s or 0.0)


def _mean_hr(activity: Activity, df: pl.DataFrame | None) -> float | None:
    if df is not None and "hr_bpm" in df.columns:
        s = df["hr_bpm"].drop_nulls()
        if len(s) > 0:
            m = _as_float(s.mean())
            if m is not None:
                return m
    if activity.avg_hr is not None:
        return float(activity.avg_hr)
    return None


def _threshold_value(thresholds: dict[str, AthleteThreshold], kind: str) -> float | None:
    t = thresholds.get(kind)
    if t is None:
        return None
    return float(t.value)


def _threshold_refs(thresholds: dict[str, AthleteThreshold]) -> dict[str, Any]:
    return {
        kind: {
            "value": float(t.value),
            "unit": t.unit,
            "valid_from": t.valid_from.isoformat(),
            "source": t.source,
        }
        for kind, t in thresholds.items()
    }


def _primary_method(sport: str, available: dict[str, Any]) -> str | None:
    if sport == "cycling" and "TSS_power" in available:
        return "TSS_power"
    if "rTSS" in available:
        return "rTSS"
    if "HRSS" in available:
        return "HRSS"
    if "TRIMP" in available:
        return "TRIMP"
    return next(iter(available), None)


def _compute_rtss(
    activity: Activity, df: pl.DataFrame, ftp_pace_mps: float
) -> dict[str, Any] | None:
    if "speed_mps" not in df.columns:
        return None
    df = add_grade_to_dataframe(df)
    speed = df["speed_mps"].to_list()
    grades = df["grade_pct"].to_list() if "grade_pct" in df.columns else [0.0] * len(speed)
    gap_values = [
        grade_adjusted_speed(s, g) for s, g in zip(speed, grades, strict=False)
    ]
    df = df.with_columns(pl.Series("gap_mps", gap_values, dtype=pl.Float64))
    gap_rolling = df["gap_mps"].rolling_mean(window_size=ROLLING_SECONDS, min_samples=1)
    df = df.with_columns(gap_rolling.alias("gap_rolling_mps"))
    ngp = _np4(df["gap_rolling_mps"])
    if ngp is None:
        return None
    ifactor = _intensity_factor(ngp, ftp_pace_mps)
    if ifactor is None:
        return None
    duration_s = _activity_duration_s(activity)
    tss = _tss_from_if(duration_s, ifactor)
    if tss is None:
        return None
    return {
        "value": tss,
        "ngp_mps": ngp,
        "ftp_pace_mps": ftp_pace_mps,
        "intensity_factor": ifactor,
        "duration_s": duration_s,
    }


def _compute_power_tss(
    activity: Activity, df: pl.DataFrame, ftp_power_w: float
) -> dict[str, Any] | None:
    if "power_w" not in df.columns:
        return None
    s = df["power_w"]
    if s.null_count() == len(s):
        return None
    rolling = s.rolling_mean(window_size=ROLLING_SECONDS, min_samples=1)
    np_value = _np4(rolling)
    if np_value is None:
        return None
    ifactor = _intensity_factor(np_value, ftp_power_w)
    if ifactor is None:
        return None
    duration_s = _activity_duration_s(activity)
    tss = _tss_from_if(duration_s, ifactor)
    if tss is None:
        return None
    return {
        "value": tss,
        "np_w": np_value,
        "ftp_power_w": ftp_power_w,
        "intensity_factor": ifactor,
        "duration_s": duration_s,
    }


def _compute_hrss_trimp(
    activity: Activity,
    df: pl.DataFrame | None,
    sex: str | None,
    hr_max: float,
    hr_rest: float,
    lthr: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    hr_avg = _mean_hr(activity, df)
    if hr_avg is None:
        return None, None
    duration_min = _activity_duration_s(activity) / 60.0
    if duration_min <= 0:
        return None, None
    r = _hr_ratio(hr_avg, hr_rest, hr_max)
    if r is None or r < 0:
        return None, None
    trimp = _trimp(duration_min, r, sex)
    lthr_r = _hr_ratio(lthr, hr_rest, hr_max)
    if lthr_r is None or lthr_r < 0:
        return None, None
    trimp_at_lthr = _trimp(60.0, lthr_r, sex)
    if trimp_at_lthr is None or trimp_at_lthr <= 0:
        return None, None
    hrss = (trimp / trimp_at_lthr) * 100.0
    hrss_meta = {
        "value": hrss,
        "trimp": trimp,
        "hr_avg": hr_avg,
        "hr_ratio": r,
        "lthr_bpm": lthr,
        "hr_max_bpm": hr_max,
        "resting_hr_bpm": hr_rest,
    }
    trimp_meta = {
        "value": trimp,
        "hr_avg": hr_avg,
        "hr_ratio": r,
        "lthr_bpm": lthr,
        "hr_max_bpm": hr_max,
        "resting_hr_bpm": hr_rest,
    }
    return hrss_meta, trimp_meta


@dataclass
class LoadResult:
    method: str
    value: float
    is_primary: bool
    inputs_hash: str
    threshold_refs: dict[str, Any] = field(default_factory=dict)


def compute_activity_load(
    activity: Activity,
    stream_path: Path,
    thresholds: dict[str, AthleteThreshold],
    athlete: Athlete,
) -> dict[str, LoadResult]:
    """Return a map of method -> LoadResult for the activity."""
    df = pl.read_parquet(stream_path)

    ftp_pace = _threshold_value(thresholds, "ftp_pace_mps")
    ftp_power = _threshold_value(thresholds, "ftp_power_w")
    lthr = _threshold_value(thresholds, "lthr_bpm")
    hr_max = _threshold_value(thresholds, "hr_max_bpm")
    hr_rest = _threshold_value(thresholds, "resting_hr_bpm")

    available: dict[str, Any] = {}
    if ftp_pace is not None and "speed_mps" in df.columns:
        rtss = _compute_rtss(activity, df, ftp_pace)
        if rtss is not None:
            available["rTSS"] = rtss

    if ftp_power is not None and "power_w" in df.columns:
        ptss = _compute_power_tss(activity, df, ftp_power)
        if ptss is not None:
            available["TSS_power"] = ptss

    if None not in (lthr, hr_max, hr_rest):
        assert lthr is not None and hr_max is not None and hr_rest is not None
        hrss, trimp = _compute_hrss_trimp(
            activity, df, athlete.sex, hr_max, hr_rest, lthr
        )
        if hrss is not None:
            available["HRSS"] = hrss
        if trimp is not None:
            available["TRIMP"] = trimp

    primary_method = _primary_method(activity.sport, available)

    inputs = {
        "activity_id": str(activity.id),
        "duration_s": _activity_duration_s(activity),
        "model_version": MODEL_VERSION,
        "thresholds": _threshold_refs(thresholds),
    }
    inputs_hash = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, default=str).encode()
    ).hexdigest()

    results: dict[str, LoadResult] = {}
    for method, data in available.items():
        results[method] = LoadResult(
            method=method,
            value=float(data["value"]),
            is_primary=(method == primary_method),
            inputs_hash=inputs_hash,
            threshold_refs={k: v for k, v in data.items() if k != "value"},
        )
    return results
