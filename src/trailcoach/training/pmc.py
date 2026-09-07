"""Performance Management Chart (CTL/ATL/TSB) calculations."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, stdev
from uuid import UUID

from sqlalchemy.orm import Session

from trailcoach.db.models import DailyLoad, TrainingMetricDaily

MODEL_VERSION = "0.1.0"
CTL_CONSTANT = 42
ATL_CONSTANT = 7


def _ema_factor(days: int) -> float:
    return 1 - math.exp(-1 / days)


def _weekly_monotony_and_strain(loads: list[float]) -> tuple[float | None, float | None]:
    if len(loads) < 2:
        return None, None
    avg = mean(loads)
    std = stdev(loads)
    if std is None or std <= 0:
        return None, None
    monotony = avg / std
    strain = sum(loads) * monotony
    return monotony, strain


def recalculate_pmc(
    db: Session,
    athlete_id: UUID,
    from_date: date | None = None,
) -> list[TrainingMetricDaily]:
    """Recompute CTL/ATL/TSB from DailyLoad rows.

    If from_date is provided, the calculation is seeded with the last metric
    row before that date; otherwise it starts from zero.
    """
    q = db.query(DailyLoad).filter(DailyLoad.athlete_id == athlete_id).order_by(DailyLoad.date)
    if from_date:
        q = q.filter(DailyLoad.date >= from_date)
    daily_rows = q.all()
    if not daily_rows:
        return []

    start = daily_rows[0].date
    end = date.today()

    load_map: dict[date, DailyLoad] = {r.date: r for r in daily_rows}

    prev = (
        db.query(TrainingMetricDaily)
        .filter(
            TrainingMetricDaily.athlete_id == athlete_id,
            TrainingMetricDaily.date < start,
        )
        .order_by(TrainingMetricDaily.date.desc())
        .first()
    )

    k_ctl = _ema_factor(CTL_CONSTANT)
    k_atl = _ema_factor(ATL_CONSTANT)

    ctl = float(prev.ctl) if prev and prev.ctl is not None else 0.0
    atl = float(prev.atl) if prev and prev.atl is not None else 0.0
    ctl_ascent = float(prev.ctl_ascent) if prev and prev.ctl_ascent is not None else 0.0
    atl_ascent = float(prev.atl_ascent) if prev and prev.atl_ascent is not None else 0.0

    ctl_history: dict[date, float] = {}
    atl_history: dict[date, float] = {}
    if prev:
        ctl_history[prev.date] = ctl
        atl_history[prev.date] = atl

    db.query(TrainingMetricDaily).filter(
        TrainingMetricDaily.athlete_id == athlete_id,
        TrainingMetricDaily.date >= start,
    ).delete(synchronize_session=False)

    out: list[TrainingMetricDaily] = []
    d = start
    while d <= end:
        dl = load_map.get(d)
        load = float(dl.load_primary) if dl and dl.load_primary is not None else 0.0
        vload = float(dl.ascent_m) if dl and dl.ascent_m is not None else 0.0

        ctl += (load - ctl) * k_ctl
        atl += (load - atl) * k_atl
        tsb = ctl - atl
        ctl_ascent += (vload - ctl_ascent) * k_ctl
        atl_ascent += (vload - atl_ascent) * k_atl

        last7 = [
            float(
                (load_map.get(d - timedelta(days=i), DailyLoad()).load_primary) or 0.0
            )
            for i in range(7)
        ]
        monotony, strain = _weekly_monotony_and_strain(last7)

        prev_ctl = ctl_history.get(d - timedelta(days=7))
        ramp_rate = (ctl - prev_ctl) if prev_ctl is not None else None

        metric = TrainingMetricDaily(
            athlete_id=athlete_id,
            date=d,
            ctl=round(ctl, 3),
            atl=round(atl, 3),
            tsb=round(tsb, 3),
            ramp_rate_7d=round(ramp_rate, 4) if ramp_rate is not None else None,
            monotony_7d=round(monotony, 4) if monotony is not None else None,
            strain_7d=round(strain, 3) if strain is not None else None,
            ctl_ascent=round(ctl_ascent, 3),
            atl_ascent=round(atl_ascent, 3),
            model_version=MODEL_VERSION,
            is_stale=False,
        )
        db.add(metric)
        out.append(metric)

        ctl_history[d] = ctl
        atl_history[d] = atl
        d += timedelta(days=1)

    return out
