from datetime import date

import numpy as np

from trailcoach.training.best_efforts import best_time_for_distance
from trailcoach.training.intensity import weekly_distribution, zone_seconds
from trailcoach.training.race_plan import build_plan, phase
from trailcoach.training.recovery import recovery_insights


def _day(d: str, **kw):
    base = {
        "date": d,
        "resting_hr": 52.0,
        "hrv_ms": 45.0,
        "hrv_baseline_low_ms": 40.0,
        "hrv_baseline_high_ms": 55.0,
        "sleep_h": 7.5,
        "sleep_score": 80,
    }
    base.update(kw)
    return base


def test_recovery_hrv_low_and_rhr_high():
    series = [_day(f"2026-11-{i:02d}") for i in range(1, 12)]
    series.append(_day("2026-11-12", hrv_ms=30.0, resting_hr=60.0))
    kinds = {(i.kind, i.level) for i in recovery_insights(series, date(2026, 11, 12))}
    assert ("hrv", "warn") in kinds
    assert ("resting_hr", "warn") in kinds


def test_recovery_ok_and_stale():
    series = [_day("2026-11-12")]
    assert [i.level for i in recovery_insights(series, date(2026, 11, 12))] == ["ok"]
    assert recovery_insights(series, date(2026, 11, 20)) == []


def test_recovery_short_sleep():
    series = [_day("2026-11-11", sleep_h=5.0), _day("2026-11-12", sleep_h=5.5)]
    assert any(i.kind == "sleep" for i in recovery_insights(series, date(2026, 11, 12)))


def test_zone_seconds_three_zones():
    t = np.arange(0, 301, 1.0)
    hr = np.concatenate([np.full(100, 120.0), np.full(100, 160.0), np.full(101, 175.0)])
    z = zone_seconds(t, hr, lthr=170)
    assert round(z["low"]) == 100 and round(z["mid"]) == 100 and round(z["high"]) == 100


def test_weekly_distribution_pct():
    out = weekly_distribution(
        [
            (date(2026, 11, 9), {"low": 3600.0, "mid": 0.0, "high": 900.0}),
            (date(2026, 11, 10), {"low": 3600.0, "mid": 0.0, "high": 900.0}),
        ]
    )
    assert out[0]["low_pct"] == 80.0 and out[0]["high_pct"] == 20.0


def test_best_time_ignores_gps_jump():
    t = np.arange(0, 1201, 1.0)
    d = t * 3.0
    d[600:] += 500.0  # salto de GPS de 500 m
    best = best_time_for_distance(t, d, 1000.0)
    assert best is not None and abs(best - 1000 / 3.0) <= 1.5
    assert best_time_for_distance(t, d, 5000.0) is None


def test_race_plan_phases():
    assert phase(5, 14)[0] == "race_week"
    assert phase(12, 14)[0] == "taper"
    assert phase(30, 14)[0] == "peak"
    assert phase(80, 14)[0] == "build"
    plan = build_plan(
        today=date(2026, 11, 13),
        race_date=date(2027, 1, 12),
        distance_km=42,
        ascent_m=2500,
        target_time_h=None,
        ctl=44,
        tsb=4,
        weekly_avg_km=45,
        weekly_avg_ascent_m=2100,
        longest_h_28d=4.5,
        longest_km_28d=30,
    )
    assert plan["days_left"] == 60 and plan["taper_start"] == "2026-12-29"
    assert plan["estimated_time_h"] == 9.6
    assert all(c["ok"] for c in plan["checks"])


def test_coach_usage_cost_and_monthly(tmp_path):
    import json

    from trailcoach.ai.coach import cost_usd, monthly_usage

    assert cost_usd("claude-sonnet-4-5", 1000, 500) == (1000 * 3 + 500 * 15) / 1e6
    assert cost_usd("desconocido", 1, 1) is None
    log = tmp_path / "u.jsonl"
    rows = [
        {
            "ts": "2026-11-01T10:00:00",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cost_usd": 0.006,
        },
        {
            "ts": "2026-11-02T10:00:00",
            "input_tokens": 900,
            "output_tokens": 300,
            "cost_usd": 0.0072,
        },
        {"ts": "2026-10-30T10:00:00", "input_tokens": 5, "output_tokens": 5, "cost_usd": 1.0},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    m = monthly_usage(log, "2026-11")
    assert m["questions"] == 2
    assert m["input_tokens"] == 1900 and m["output_tokens"] == 500
    assert m["cost_usd"] == 0.0132


def test_decoupling_pct():
    import numpy as np

    from trailcoach.training.intensity import decoupling_pct

    t = np.arange(0, 3600, 1.0)
    v = np.full_like(t, 3.0)
    hr = np.where(t < 1800, 140.0, 154.0)
    assert decoupling_pct(t, v, hr) == round((1 - 140 / 154) * 100, 1)
    assert decoupling_pct(t[:600], v[:600], hr[:600]) is None
