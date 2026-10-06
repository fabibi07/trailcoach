"""F4: per-source activity metrics and zone times.

All FIT payloads are synthetic (`tests/fit_builder.py`).
"""

from pathlib import Path

import pytest
from click.testing import CliRunner
from fit_builder import SyntheticActivity, build_fit

from trailcoach.db.models import (
    Activity,
    ActivitySourceMetric,
    ActivityZoneTime,
    Athlete,
    AthleteSourceAccount,
    SourceActivity,
)
from trailcoach.ingest.orchestrator import IngestionOrchestrator
from trailcoach.ingest.reprocess import reprocess_activity_metrics
from trailcoach.providers.fit_parser import FitParser
from trailcoach.sources import GarminFitProvider, MockSourceProvider

RICH = SyntheticActivity(
    calories_kcal=420,
    training_effect_x10=32,
    anaerobic_training_effect_x10=11,
    rpe_x10=60,
    hr_zone_seconds=[60, 300, 900, 500, 40],
    hr_zone_bounds=[120, 140, 155, 170, 185],
    threshold_hr_bpm=168,
)


@pytest.fixture
def athlete(db):
    a = Athlete(display_name="Metrics Athlete")
    db.add(a)
    db.flush()
    return a


@pytest.fixture
def fit_dir(tmp_path) -> Path:
    d = tmp_path / "export"
    d.mkdir()
    return d


@pytest.fixture
def account(db, athlete, fit_dir):
    acc = AthleteSourceAccount(
        athlete_id=athlete.id, source="garmin_fit", auth_json={"import_dir": str(fit_dir)}
    )
    db.add(acc)
    db.flush()
    return acc


def _metrics(db) -> dict[str, float]:
    return {m.metric: float(m.value) for m in db.query(ActivitySourceMetric)}


def test_parser_maps_session_fields_to_neutral_metrics():
    parser = FitParser(source="garmin_fit")
    doc = parser.parse_bytes(build_fit(RICH))

    metrics = {m.metric: m for m in parser.build_source_metrics(doc)}
    assert {k: float(m.value) for k, m in metrics.items()} == {
        "calories_kcal": 420.0,
        "aerobic_training_effect": 3.2,
        "anaerobic_training_effect": 1.1,
        "rpe": 6.0,
        "device_lthr_bpm": 168.0,
    }
    assert metrics["rpe"].source_field == "workout_rpe"
    assert all(m.value_type == "native" and m.source == "garmin_fit" for m in metrics.values())

    zones = parser.build_zone_times(doc)
    assert [(z.zone_kind, z.zone_index, z.seconds, z.upper_bound) for z in zones] == [
        ("heart_rate", 0, 60.0, 120.0),
        ("heart_rate", 1, 300.0, 140.0),
        ("heart_rate", 2, 900.0, 155.0),
        ("heart_rate", 3, 500.0, 170.0),
        ("heart_rate", 4, 40.0, 185.0),
    ]


def test_parser_returns_nothing_when_fit_lacks_fields():
    parser = FitParser(source="garmin_fit")
    doc = parser.parse_bytes(build_fit(SyntheticActivity()))
    assert parser.build_source_metrics(doc) == []
    assert parser.build_zone_times(doc) == []


def test_schema_has_no_provider_specific_columns():
    for model in (ActivitySourceMetric, ActivityZoneTime):
        names = [c.name for c in model.__table__.columns]
        assert not [n for n in names if "garmin" in n or "fit" in n]


def test_orchestrator_persists_metrics_and_zones_idempotently(db, account, fit_dir):
    (fit_dir / "run.fit").write_bytes(build_fit(RICH))
    orchestrator = IngestionOrchestrator(db)
    orchestrator.run(account, GarminFitProvider())
    orchestrator.run(account, GarminFitProvider())
    db.commit()

    activity = db.query(Activity).one()
    sa = db.query(SourceActivity).one()
    rows = db.query(ActivitySourceMetric).all()
    assert len(rows) == 5
    assert {(r.activity_id, r.source_activity_id, r.source) for r in rows} == {
        (activity.id, sa.id, "garmin_fit")
    }
    assert _metrics(db)["calories_kcal"] == 420.0
    assert db.query(ActivityZoneTime).filter_by(activity_id=activity.id).count() == 5


def test_two_sources_keep_their_own_value_for_the_same_metric(db, athlete, account, fit_dir):
    (fit_dir / "run.fit").write_bytes(build_fit(RICH))
    IngestionOrchestrator(db).run(account, GarminFitProvider())
    db.flush()
    activity = db.query(Activity).one()
    other = SourceActivity(
        athlete_id=athlete.id,
        source="source_a",
        source_activity_id="ext-1",
        canonical_activity_id=activity.id,
        start_time_utc=activity.start_time_utc,
    )
    db.add(other)
    db.flush()
    db.add(
        ActivitySourceMetric(
            activity_id=activity.id,
            source_activity_id=other.id,
            source="source_a",
            metric="calories_kcal",
            value=455,
            unit="kcal",
            value_type="estimated",
        )
    )
    db.commit()

    by_source = {
        r.source: float(r.value)
        for r in db.query(ActivitySourceMetric).filter_by(
            activity_id=activity.id, metric="calories_kcal"
        )
    }
    assert by_source == {"garmin_fit": 420.0, "source_a": 455.0}


def test_reprocess_rebuilds_metrics_from_raw(db, athlete, account, fit_dir):
    (fit_dir / "run.fit").write_bytes(build_fit(RICH))
    IngestionOrchestrator(db).run(account, GarminFitProvider())
    db.query(ActivitySourceMetric).delete()
    db.query(ActivityZoneTime).delete()
    db.flush()
    (fit_dir / "run.fit").unlink()

    first = reprocess_activity_metrics(db, GarminFitProvider(), athlete.id)
    second = reprocess_activity_metrics(db, GarminFitProvider(), athlete.id)
    db.commit()

    assert (first.processed, first.metrics, first.zone_times) == (1, 5, 5)
    assert (second.processed, second.failed) == (1, 0)
    assert db.query(ActivitySourceMetric).count() == 5
    assert db.query(ActivityZoneTime).count() == 5


def test_reprocess_is_noop_for_sources_without_reparse(db, athlete):
    assert MockSourceProvider().payload_from_raw(b"raw", "x") is None
    summary = reprocess_activity_metrics(db, MockSourceProvider(), athlete.id)
    assert summary.processed == 0 and summary.failed == 0


def test_cli_reprocess_raw(monkeypatch, db, athlete, account, fit_dir):
    import trailcoach.cli.main as cli_main

    (fit_dir / "run.fit").write_bytes(build_fit(RICH))
    IngestionOrchestrator(db).run(account, GarminFitProvider())
    db.flush()
    monkeypatch.setattr(cli_main, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    runner = CliRunner()
    result = runner.invoke(
        cli_main.cli,
        ["reprocess-raw", "--athlete-id", str(athlete.id), "--source", "garmin_fit"],
    )
    assert result.exit_code == 0, result.output
    assert "reprocessed=1" in result.output and "metrics=5" in result.output

    bad = runner.invoke(
        cli_main.cli, ["reprocess-raw", "--athlete-id", str(athlete.id), "--source", "nope"]
    )
    assert bad.exit_code != 0
