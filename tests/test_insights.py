"""Tests de la interpretación del estado del atleta (insights del dashboard)."""

from datetime import date

from trailcoach.training.insights import (
    acwr_insight,
    build_insights,
    form_insight,
    inactivity_insight,
    ramp_insight,
    threshold_pace_insight,
)


def test_form_zones():
    assert form_insight(-35).title == "Sobrecarga"
    assert form_insight(-15).title == "Bloque de carga"
    assert form_insight(0).title == "Equilibrio"
    assert form_insight(15).title == "Fresco"
    assert form_insight(30).title == "Muy descansado"
    assert form_insight(None) is None


def test_acwr_levels():
    assert acwr_insight(400, 1000).level == "alert"  # 1.6
    assert acwr_insight(350, 1000).level == "warn"  # 1.4
    assert acwr_insight(250, 1000).level == "ok"  # 1.0
    assert acwr_insight(150, 1000).level == "info"  # 0.6
    assert acwr_insight(0, 1000) is None


def test_ramp_levels():
    assert ramp_insight(9).level == "alert"
    assert ramp_insight(6).level == "warn"
    assert ramp_insight(2).level == "ok"
    assert ramp_insight(-2).level == "info"
    assert ramp_insight(-6).level == "warn"


def test_threshold_pace_stale_only_when_much_slower_and_not_manual():
    # 5:50/km frente a 4:53/km del último año → aviso
    slow, best = 1000 / 350, 1000 / 293
    ins = threshold_pace_insight(slow, best, "derived")
    assert ins is not None and "5:50" in ins.message and "4:53" in ins.message
    assert threshold_pace_insight(slow, best, "manual") is None
    assert threshold_pace_insight(best * 0.98, best, "derived") is None


def test_inactivity():
    today = date(2026, 11, 10)
    assert inactivity_insight(date(2026, 11, 8), today) is None
    assert inactivity_insight(date(2026, 11, 1), today).level == "info"


def test_build_insights_sorted_by_urgency():
    out = build_insights(
        today=date(2026, 11, 10),
        tsb=0,
        ramp_rate_7d=9,
        load_7d=250,
        load_28d=1000,
    )
    assert [i["level"] for i in out] == ["alert", "ok", "ok"]
    assert set(out[0]) == {"kind", "level", "title", "message"}
