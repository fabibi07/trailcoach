"""Estimación de umbrales del atleta a partir de sus datos (funciones puras).

No accede a la base de datos ni conoce fuentes concretas: recibe evidencia por
actividad (streams y ajustes `device_*` neutrales), FC de reposo diaria y
devuelve candidatos mensuales y la selección efectiva (D1–D7 de P0.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any

import numpy as np

HR_MIN_VALID = 30.0
HR_MAX_VALID = 220.0
HR_MAX_JUMP_BPM_S = 8.0
MAX_GAP_S = 10.0

HR_MAX_WINDOW_S = 30.0
LTHR_WINDOW_S = 1200.0
LTHR_FROM_20MIN = 0.95
PACE_HR_TOLERANCE = 3.0
PACE_MIN_SEGMENT_S = 480.0
PACE_MIN_SEGMENTS = 3

HR_MAX_LOOKBACK_DAYS = 365
HR_MAX_MIN_ACTIVITIES = 10
LTHR_LOOKBACK_DAYS = 90
PACE_LOOKBACK_DAYS = 90
RESTING_MIN_DAYS = 7
LTHR_WARN_DIFF_BPM = 5.0

RUN_SPORTS = {"running", "trail_running"}
NON_ROAD_SUB_SPORTS = {"treadmill", "indoor_running", "virtual_activity", "trail"}

DEVICE_METRICS = {
    "hr_max_bpm": "device_hr_max_bpm",
    "lthr_bpm": "device_lthr_bpm",
    "resting_hr_bpm": "device_resting_hr_bpm",
    "ftp_power_w": "device_ftp_power_w",
}
UNITS = {
    "hr_max_bpm": "bpm",
    "lthr_bpm": "bpm",
    "resting_hr_bpm": "bpm",
    "ftp_power_w": "W",
    "ftp_pace_mps": "m/s",
}
# D1: orden de preferencia entre candidatos automáticos (los manuales siempre ganan).
# LTHR prefiere el valor del dispositivo (D3); el derivado solo valida.
PREFERENCE = {
    "hr_max_bpm": ["derived", "device"],
    "lthr_bpm": ["device", "derived"],
    "resting_hr_bpm": ["derived", "device"],
    "ftp_power_w": ["device"],
    "ftp_pace_mps": ["derived"],
}
# D7: cambio mínimo para versionar (absoluto en ppm, relativo en el resto).
MIN_CHANGE_ABS = {"hr_max_bpm": 2.0, "lthr_bpm": 2.0, "resting_hr_bpm": 2.0}
MIN_CHANGE_REL = 0.02


@dataclass
class ActivityEvidence:
    """Resumen de una actividad para estimar umbrales."""

    day: date
    sport: str
    sub_sport: str | None = None
    max_hr_30s: float | None = None
    best_hr_20min: float | None = None
    device: dict[str, float] = field(default_factory=dict)
    t_s: np.ndarray | None = None
    hr_bpm: np.ndarray | None = None
    speed_mps: np.ndarray | None = None

    @property
    def is_run(self) -> bool:
        return self.sport in RUN_SPORTS

    @property
    def is_road_run(self) -> bool:
        return self.sport == "running" and (self.sub_sport or "") not in NON_ROAD_SUB_SPORTS


@dataclass
class Candidate:
    kind: str
    valid_from: date
    value: float
    unit: str
    method: str
    source: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class EstimationResult:
    candidates: list[Candidate] = field(default_factory=list)
    effective: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------
def guard_hr(t_s: np.ndarray, hr: np.ndarray) -> np.ndarray:
    """D6: máscara de muestras de pulso plausibles (no modifica el stream)."""
    ok: np.ndarray = np.isfinite(hr) & (hr >= HR_MIN_VALID) & (hr <= HR_MAX_VALID)
    if len(hr) > 1:
        dt = np.diff(t_s)
        rate = np.abs(np.diff(hr)) / np.where(dt > 0, dt, np.inf)
        ok[1:] &= rate <= HR_MAX_JUMP_BPM_S
    return ok


def best_mean(t_s: np.ndarray, values: np.ndarray, window_s: float) -> float | None:
    """Mayor media ponderada por tiempo en una ventana continua de `window_s`.

    Las ventanas con huecos mayores a `MAX_GAP_S` se descartan.
    """
    if len(t_s) < 2:
        return None
    dt = np.diff(t_s)
    seg = np.minimum(dt, MAX_GAP_S)
    area = np.concatenate([[0.0], np.cumsum(values[:-1] * seg)])
    covered = np.concatenate([[0.0], np.cumsum(seg)])
    gaps = np.concatenate([[0], np.cumsum(dt > MAX_GAP_S)])
    ends = np.searchsorted(t_s, t_s + window_s, side="left")
    best: float | None = None
    for i, j in enumerate(ends):
        if j >= len(t_s):
            break
        if gaps[j] != gaps[i]:
            continue
        span = covered[j] - covered[i]
        if span <= 0:
            continue
        mean = (area[j] - area[i]) / span
        if best is None or mean > best:
            best = float(mean)
    return best


def hr_summary(t_s: np.ndarray, hr: np.ndarray) -> tuple[float | None, float | None]:
    """(máx de 30 s, mejor media de 20 min) sobre el pulso con guarda D6."""
    ok = guard_hr(t_s, hr)
    t, h = t_s[ok], hr[ok]
    return best_mean(t, h, HR_MAX_WINDOW_S), best_mean(t, h, LTHR_WINDOW_S)


def threshold_pace_segments(
    t_s: np.ndarray, hr: np.ndarray, speed: np.ndarray, lthr: float
) -> list[float]:
    """Velocidad media de tramos continuos ≥ 8 min con FC en LTHR ± 3 (D5)."""
    ok = guard_hr(t_s, hr) & np.isfinite(speed) & (np.abs(hr - lthr) <= PACE_HR_TOLERANCE)
    speeds: list[float] = []
    start: int | None = None
    for i in range(len(t_s) + 1):
        inside = i < len(t_s) and ok[i] and (start is None or t_s[i] - t_s[i - 1] <= MAX_GAP_S)
        if inside and start is None:
            start = i
        elif not inside and start is not None:
            end = i - 1
            if t_s[end] - t_s[start] >= PACE_MIN_SEGMENT_S:
                dt = np.diff(t_s[start : end + 1])
                speeds.append(float(np.sum(speed[start:end] * dt) / np.sum(dt)))
            start = i if i < len(t_s) and ok[i] else None
    return speeds


# ---------------------------------------------------------------------------
# Candidatos mensuales
# ---------------------------------------------------------------------------
def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def months_between(first: date, last: date) -> list[date]:
    out, m = [], month_start(first)
    while m <= last:
        out.append(m)
        m = next_month(m)
    return out


def _window(evidence: Sequence[ActivityEvidence], end: date, days: int) -> list[ActivityEvidence]:
    start = end - timedelta(days=days)
    return [e for e in evidence if start < e.day <= end]


def _device_candidates(month: date, acts: list[ActivityEvidence]) -> list[Candidate]:
    out = []
    for kind, metric in DEVICE_METRICS.items():
        values = [e.device[metric] for e in acts if e.device.get(metric, 0) > 0]
        if values:
            out.append(
                Candidate(
                    kind,
                    month,
                    float(median(values)),
                    UNITS[kind],
                    "device_setting",
                    "device",
                    0.7,
                    {"metric": metric, "n_activities": len(values)},
                )
            )
    return out


def estimate(
    evidence: Iterable[ActivityEvidence],
    resting_hr_by_day: dict[date, float],
) -> EstimationResult:
    """Candidatos mensuales y selección efectiva versionada (D1–D7)."""
    acts = sorted(evidence, key=lambda e: e.day)
    days = [e.day for e in acts] + list(resting_hr_by_day)
    result = EstimationResult()
    if not days:
        return result
    monthly_lthr: dict[date, float] = {}
    for month in months_between(min(days), max(days)):
        end = next_month(month) - timedelta(days=1)
        in_month = [e for e in acts if month <= e.day <= end]
        cands = _device_candidates(month, in_month)

        runs = [e for e in _window(acts, end, HR_MAX_LOOKBACK_DAYS) if e.is_run and e.max_hr_30s]
        # Con poco historial el máximo observado subestima la FC máx: se usa el dispositivo.
        if len(runs) >= HR_MAX_MIN_ACTIVITIES:
            best = max(runs, key=lambda e: e.max_hr_30s or 0)
            cands.append(
                Candidate(
                    "hr_max_bpm",
                    month,
                    round(float(best.max_hr_30s or 0), 1),
                    "bpm",
                    "max_30s_365d",
                    "derived",
                    0.8,
                    {"n_activities": len(runs), "best_day": best.day.isoformat()},
                )
            )
        runs20 = [e for e in _window(acts, end, LTHR_LOOKBACK_DAYS) if e.is_run and e.best_hr_20min]
        if runs20:
            best20 = max(e.best_hr_20min or 0 for e in runs20)
            cands.append(
                Candidate(
                    "lthr_bpm",
                    month,
                    round(best20 * LTHR_FROM_20MIN, 1),
                    "bpm",
                    "best_20min_x0.95_90d",
                    "derived",
                    0.5,
                    {"n_activities": len(runs20), "best_20min_bpm": round(best20, 1)},
                )
            )
        rest = [v for d, v in resting_hr_by_day.items() if month <= d <= end]
        if len(rest) >= RESTING_MIN_DAYS:
            cands.append(
                Candidate(
                    "resting_hr_bpm",
                    month,
                    float(median(rest)),
                    "bpm",
                    "wellness_median_month",
                    "derived",
                    0.8,
                    {"n_days": len(rest), "min_bpm": min(rest)},
                )
            )

        lthr_pick = _pick([c for c in cands if c.kind == "lthr_bpm"], "lthr_bpm")
        if lthr_pick is not None:
            monthly_lthr[month] = lthr_pick.value
        lthr = monthly_lthr.get(month) or _last(monthly_lthr)
        device = next((c for c in cands if c.kind == "lthr_bpm" and c.source == "device"), None)
        derived = next((c for c in cands if c.kind == "lthr_bpm" and c.source == "derived"), None)
        if device and derived and abs(device.value - derived.value) > LTHR_WARN_DIFF_BPM:
            result.warnings.append(
                f"{month:%Y-%m}: LTHR dispositivo {device.value:.0f} vs derivado "
                f"{derived.value:.0f} (> {LTHR_WARN_DIFF_BPM:.0f} ppm)"
            )
        if lthr is not None:
            pace = _pace_candidate(month, _window(acts, end, PACE_LOOKBACK_DAYS), lthr)
            if pace is not None:
                cands.append(pace)
        result.candidates.extend(cands)
    result.effective = select_effective(result.candidates)
    return result


def _last(values: dict[date, float]) -> float | None:
    return values[max(values)] if values else None


def _pace_candidate(month: date, acts: list[ActivityEvidence], lthr: float) -> Candidate | None:
    speeds: list[float] = []
    for e in acts:
        if e.is_road_run and e.t_s is not None and e.hr_bpm is not None and e.speed_mps is not None:
            speeds += threshold_pace_segments(e.t_s, e.hr_bpm, e.speed_mps, lthr)
    if len(speeds) < PACE_MIN_SEGMENTS:
        return None
    return Candidate(
        "ftp_pace_mps",
        month,
        round(float(median(speeds)), 4),
        "m/s",
        "lthr_segments_90d",
        "derived",
        0.6,
        {"n_segments": len(speeds), "lthr_bpm": lthr},
    )


def _pick(cands: list[Candidate], kind: str) -> Candidate | None:
    order = PREFERENCE.get(kind, [])
    ranked = sorted((c for c in cands if c.source in order), key=lambda c: order.index(c.source))
    return ranked[0] if ranked else None


def _changed(kind: str, old: float, new: float) -> bool:
    if kind in MIN_CHANGE_ABS:
        return abs(new - old) >= MIN_CHANGE_ABS[kind]
    return abs(new - old) >= MIN_CHANGE_REL * abs(old)


def select_effective(candidates: Iterable[Candidate]) -> list[Candidate]:
    """D1 + D7: un valor por mes según preferencia, versionado solo si cambia."""
    by_key: dict[tuple[str, date], list[Candidate]] = {}
    for c in candidates:
        by_key.setdefault((c.kind, c.valid_from), []).append(c)
    effective: list[Candidate] = []
    last: dict[str, Candidate] = {}
    for kind, month in sorted(by_key):
        pick = _pick(by_key[(kind, month)], kind)
        if pick is None:
            continue
        prev = last.get(kind)
        if prev is None or _changed(kind, prev.value, pick.value):
            effective.append(pick)
            last[kind] = pick
    return effective
