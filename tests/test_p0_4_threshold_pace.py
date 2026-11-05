"""Tests sintéticos de P0.4: ritmo umbral, FTP de potencia y carga principal."""

from datetime import date, timedelta

import numpy as np
import pytest
from click.testing import CliRunner

from trailcoach.cli import main as cli_main
from trailcoach.training.load import _primary_method
from trailcoach.training.threshold_estimation import (
    ActivityEvidence,
    Candidate,
    _best20_candidates,
    best_gap_20min,
    guard_speed,
    select_effective,
)


def test_guard_speed_descarta_picos_gps():
    t = np.arange(6, dtype=float)
    v = np.array([3.0, 3.1, 7.5, 3.2, 3.1, 6.9])
    assert guard_speed(t, v).tolist() == [True, True, False, False, True, False]


def test_best_gap_ignora_pico_gps():
    t = np.arange(0, 1800, dtype=float)
    v = np.full(1800, 3.0)
    v[900] = 9.0
    assert best_gap_20min(t, v, v.copy()) == pytest.approx(3.0)


def _ev(day, gap, power=None, sub_sport=None, sport="running"):
    return ActivityEvidence(
        day=day, sport=sport, sub_sport=sub_sport, best_gap_20min=gap, best_power_20min=power
    )


def test_best20_requiere_tres_carreras_al_aire_libre():
    d = date(2026, 1, 10)
    acts = [_ev(d, 3.4, 400), _ev(d, 3.2, 380), _ev(d, 5.0, 500, sub_sport="treadmill")]
    assert _best20_candidates(date(2026, 1, 1), acts) == []
    acts.append(_ev(d - timedelta(days=3), 3.0, 360, sport="trail_running"))
    out = {c.kind: c for c in _best20_candidates(date(2026, 1, 1), acts)}
    assert out["ftp_pace_mps"].value == pytest.approx(3.4 * 0.95)
    assert out["ftp_pace_mps"].method == "gap_20min_x0.95_90d"
    assert out["ftp_power_w"].value == pytest.approx(380.0)
    assert out["ftp_pace_mps"].evidence["n_activities"] == 3


def _cand(kind, value, source, method):
    return Candidate(kind, date(2026, 1, 1), value, "u", method, source, 0.5)


def test_preferencia_por_metodo():
    eff = select_effective(
        [
            _cand("ftp_power_w", 410, "device", "device_setting"),
            _cand("ftp_power_w", 370, "derived", "power_20min_x0.95_90d"),
            _cand("ftp_pace_mps", 3.2, "derived", "gap_20min_x0.95_90d"),
            _cand("ftp_pace_mps", 3.3, "derived", "lthr_segments_90d"),
        ]
    )
    got = {c.kind: c.value for c in eff}
    assert got == {"ftp_power_w": 370, "ftp_pace_mps": 3.3}


def test_carga_principal_por_deporte():
    both = {"rTSS": 1.0, "HRSS": 2.0}
    assert _primary_method("trail_running", both) == "HRSS"
    assert _primary_method("running", both) == "rTSS"
    assert _primary_method("walking", both) == "HRSS"
    assert _primary_method("trail_running", {"rTSS": 1.0}) == "rTSS"


def test_cli_sync_daily_encadena_pasos(monkeypatch):
    calls = []
    for name in ("ingest_garmin_connect", "estimate_thresholds_cmd", "recalculate"):
        cmd = getattr(cli_main, name)
        monkeypatch.setattr(cmd, "callback", lambda n=name, **kw: calls.append((n, kw)))
    res = CliRunner().invoke(cli_main.cli, ["sync-daily", "--athlete-id", "x"])
    assert res.exit_code == 0, res.output
    assert [c[0] for c in calls] == [
        "ingest_garmin_connect",
        "estimate_thresholds_cmd",
        "recalculate",
    ]
    assert calls[0][1]["incremental"] is True
    assert calls[1][1]["apply_"] is True
