"""Tests sintéticos de P0.3: estimación de umbrales (sin datos personales)."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest
from click.testing import CliRunner

from trailcoach.cli import main as cli_main
from trailcoach.core.config import settings
from trailcoach.db.models import (
    Activity,
    ActivitySourceMetric,
    ActivityStreamSet,
    Athlete,
    AthleteThreshold,
    SourceActivity,
    ThresholdEstimate,
    WellnessDaily,
)
from trailcoach.training import threshold_estimation as te
from trailcoach.training.threshold_estimation import (
    ActivityEvidence,
    Candidate,
    best_mean,
    estimate,
    guard_hr,
    hr_summary,
    select_effective,
    threshold_pace_segments,
)
from trailcoach.training.thresholds import estimate_and_apply, set_threshold


def _const(n: int, value: float) -> np.ndarray:
    return np.full(n, value, dtype=float)


def test_guard_hr_descarta_rangos_y_saltos():
    t = np.arange(6, dtype=float)
    hr = np.array([140, 141, 230, 142, 20, 143], dtype=float)
    ok = guard_hr(t, hr)
    assert ok.tolist() == [True, True, False, False, False, False]
    hr2 = np.array([140, 141, 170, 142, 143, 144], dtype=float)
    assert guard_hr(t, hr2).tolist() == [True, True, False, False, True, True]


def test_best_mean_ventana_y_huecos():
    t = np.arange(0, 100, dtype=float)
    hr = np.concatenate([_const(50, 140), _const(50, 160)])
    assert best_mean(t, hr, 30) == pytest.approx(160)
    assert best_mean(t, hr, 200) is None
    t_gap = np.concatenate([np.arange(0, 20), np.arange(60, 80)]).astype(float)
    assert best_mean(t_gap, _const(40, 150), 30) is None


def test_hr_summary_ignora_pico_aislado():
    t = np.arange(0, 1800, dtype=float)
    hr = _const(1800, 150)
    hr[900] = 215
    max30, best20 = hr_summary(t, hr)
    assert max30 == pytest.approx(150)
    assert best20 == pytest.approx(150)


def test_tramos_ritmo_umbral():
    t = np.arange(0, 1500, dtype=float)
    hr = np.concatenate([_const(300, 140), _const(600, 171), _const(600, 140)])
    speed = np.concatenate([_const(300, 3.0), _const(600, 3.6), _const(600, 3.0)])
    segs = threshold_pace_segments(t, hr, speed, lthr=170)
    assert segs == [pytest.approx(3.6)]
    assert threshold_pace_segments(t, hr, speed, lthr=160) == []


def _cand(kind, month, value, source, method="m"):
    return Candidate(kind, month, value, "bpm", method, source, 0.5)


def test_preferencia_y_versionado():
    m1, m2, m3 = date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)
    cands = [
        _cand("hr_max_bpm", m1, 186, "derived"),
        _cand("hr_max_bpm", m1, 192, "device"),
        _cand("lthr_bpm", m1, 165, "derived"),
        _cand("lthr_bpm", m1, 170, "device"),
        _cand("lthr_bpm", m2, 171, "device"),
        _cand("lthr_bpm", m3, 173, "device"),
    ]
    eff = select_effective(cands)
    hrmax = [c for c in eff if c.kind == "hr_max_bpm"]
    lthr = [c for c in eff if c.kind == "lthr_bpm"]
    assert [(c.value, c.source) for c in hrmax] == [(186, "derived")]
    assert [(c.valid_from, c.value) for c in lthr] == [(m1, 170), (m3, 173)]


def _run_evidence(day, hr_level, device_lthr=None, road=True):
    n = 2400
    t = np.arange(n, dtype=float)
    hr = np.concatenate([_const(600, 140), _const(n - 600, hr_level)])
    speed = np.concatenate([_const(600, 3.0), _const(n - 600, 3.5)])
    max30, best20 = hr_summary(t, hr)
    ev = ActivityEvidence(
        day=day,
        sport="running" if road else "trail_running",
        max_hr_30s=max30,
        best_hr_20min=best20,
        device={"device_lthr_bpm": device_lthr} if device_lthr else {},
    )
    if road:
        ev.t_s, ev.hr_bpm, ev.speed_mps = t, hr, speed
    return ev


@pytest.fixture
def few_runs_ok(monkeypatch):
    monkeypatch.setattr(te, "HR_MAX_MIN_ACTIVITIES", 1)


def test_fc_max_requiere_historial_minimo():
    ev = [_run_evidence(date(2025, 4, d), 180) for d in (3, 10)]
    assert not [c for c in estimate(ev, {}).candidates if c.kind == "hr_max_bpm"]


def test_estimate_mensual_completo(few_runs_ok):
    ev = [_run_evidence(date(2025, 4, d), 170, device_lthr=170) for d in (3, 10, 17)]
    ev.append(_run_evidence(date(2025, 4, 20), 182, road=False))
    rest = {date(2025, 4, 1) + timedelta(days=i): 50.0 + (i % 5) for i in range(20)}
    res = estimate(ev, rest)
    eff = {c.kind: c for c in res.effective}
    assert eff["hr_max_bpm"].value == pytest.approx(182)
    assert eff["lthr_bpm"].source == "device" and eff["lthr_bpm"].value == 170
    assert eff["resting_hr_bpm"].value == 52
    assert eff["ftp_pace_mps"].value == pytest.approx(3.5)
    assert eff["ftp_pace_mps"].evidence["n_segments"] == 3
    # El derivado (182 × 0.95 ≈ 172.9) difiere < 5 ppm: sin aviso.
    assert res.warnings == []


def test_ritmo_requiere_tres_tramos_y_aviso_lthr():
    ev = [_run_evidence(date(2025, 4, d), 170, device_lthr=150) for d in (3, 10)]
    res = estimate(ev, {})
    kinds = {c.kind for c in res.candidates}
    assert "ftp_pace_mps" not in kinds
    assert len(res.warnings) == 1 and "LTHR" in res.warnings[0]


# ---------------------------------------------------------------------------
# Persistencia y CLI
# ---------------------------------------------------------------------------
def _seed(db):
    athlete = Athlete(display_name="Sintético")
    db.add(athlete)
    db.flush()
    for i, day in enumerate((3, 10, 17)):
        start = datetime(2025, 4, day, 12, tzinfo=timezone.utc)
        act = Activity(
            athlete_id=athlete.id,
            start_time_utc=start,
            sport="running",
            primary_source="fake",
            duration_elapsed_s=1800,
        )
        db.add(act)
        db.flush()
        sa = SourceActivity(
            athlete_id=athlete.id,
            source="fake",
            source_activity_id=f"a{i}",
            canonical_activity_id=act.id,
        )
        db.add(sa)
        db.flush()
        db.add(
            ActivitySourceMetric(
                activity_id=act.id,
                source_activity_id=sa.id,
                source="fake",
                metric="device_lthr_bpm",
                value=170,
                unit="bpm",
            )
        )
        n = 1800
        df = pl.DataFrame(
            {
                "t_s": np.arange(n, dtype=float),
                "hr_bpm": np.concatenate([_const(600, 140), _const(n - 600, 170)]),
                "speed_mps": np.concatenate([_const(600, 3.0), _const(n - 600, 3.5)]),
            }
        )
        rel = f"fake/streams/{i}.parquet"
        path = settings.processed_root_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)
        db.add(
            ActivityStreamSet(
                activity_id=act.id,
                source_activity_id=sa.id,
                storage_path=rel,
                sample_count=n,
            )
        )
    for i in range(20):
        db.add(
            WellnessDaily(
                athlete_id=athlete.id,
                date=date(2025, 4, 1) + timedelta(days=i),
                source="fake",
                resting_hr=50,
            )
        )
    db.flush()
    return athlete


def _snapshot(db, athlete_id):
    rows = db.query(AthleteThreshold).filter_by(athlete_id=athlete_id).all()
    return sorted((r.kind, r.valid_from, float(r.value), r.source) for r in rows)


def test_apply_idempotente_y_trazable(db, few_runs_ok):
    athlete = _seed(db)
    dry = estimate_and_apply(db, athlete.id)
    assert dry.written and not dry.applied
    assert db.query(AthleteThreshold).count() == 0

    estimate_and_apply(db, athlete.id, apply=True)
    first = _snapshot(db, athlete.id)
    n_est = db.query(ThresholdEstimate).count()
    kinds = {k for k, *_ in first}
    assert {"hr_max_bpm", "lthr_bpm", "resting_hr_bpm", "ftp_pace_mps"} <= kinds
    for kind, valid_from, value, _src in first:
        est = db.query(ThresholdEstimate).filter_by(kind=kind, valid_from=valid_from).all()
        assert any(float(e.value) == value and e.evidence for e in est)

    estimate_and_apply(db, athlete.id, apply=True)
    assert _snapshot(db, athlete.id) == first
    assert db.query(ThresholdEstimate).count() == n_est


def test_manual_no_se_sobrescribe(db):
    athlete = _seed(db)
    set_threshold(db, athlete.id, "lthr_bpm", 168, "bpm", date(2025, 4, 1))
    summary = estimate_and_apply(db, athlete.id, apply=True)
    assert summary.blocked_by_manual == 1
    lthr = db.query(AthleteThreshold).filter_by(athlete_id=athlete.id, kind="lthr_bpm").all()
    assert [(float(r.value), r.source) for r in lthr] == [(168, "manual")]


def test_cli_estimate_thresholds(db, monkeypatch):
    athlete = _seed(db)
    db.commit()
    monkeypatch.setattr(cli_main, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    res = CliRunner().invoke(cli_main.cli, ["estimate-thresholds", "--athlete-id", str(athlete.id)])
    assert res.exit_code == 0, res.output
    assert "Simulación" in res.output
    res = CliRunner().invoke(
        cli_main.cli, ["estimate-thresholds", "--athlete-id", str(athlete.id), "--apply"]
    )
    assert res.exit_code == 0, res.output
    assert db.query(AthleteThreshold).filter_by(source="device").count() == 1
