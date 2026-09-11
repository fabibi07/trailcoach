"""P0.0 provider-agnostic multi-source architecture tests."""

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trailcoach.db.models import (
    Activity,
    Athlete,
    AthleteSourceAccount,
    MetricCapability,
    RawFile,
    SourceActivity,
    SourcePriority,
    WellnessDaily,
)
from trailcoach.dedup.engine import DedupeEngine
from trailcoach.ingest.raw_store import RawStore
from trailcoach.provenance import DataLineage
from trailcoach.sources import (
    MockSourceProvider,
    SourceProviderRegistry,
)


@pytest.fixture
def athlete(db):
    a = Athlete(display_name="Test Athlete")
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Multi-source wellness
# ---------------------------------------------------------------------------
def test_multi_source_wellness_does_not_overwrite(db, athlete):
    day = date(2024, 1, 1)
    w1 = WellnessDaily(
        athlete_id=athlete.id,
        date=day,
        source="source_a",
        resting_hr=50,
        hrv_ms=45.0,
    )
    w2 = WellnessDaily(
        athlete_id=athlete.id,
        date=day,
        source="source_b",
        resting_hr=51,
        hrv_ms=46.0,
    )
    db.add_all([w1, w2])
    db.commit()

    rows = (
        db.query(WellnessDaily)
        .filter_by(athlete_id=athlete.id, date=day)
        .order_by(WellnessDaily.source)
        .all()
    )

    assert len(rows) == 2
    assert {r.source for r in rows} == {"source_a", "source_b"}
    assert {float(r.hrv_ms or 0) for r in rows} == {45.0, 46.0}


# ---------------------------------------------------------------------------
# Source / Device model
# ---------------------------------------------------------------------------
def test_athlete_source_account_is_per_athlete(db):
    a1 = Athlete(display_name="A1")
    a2 = Athlete(display_name="A2")
    db.add_all([a1, a2])
    db.flush()

    acc1 = AthleteSourceAccount(athlete_id=a1.id, source="source_a")
    acc2 = AthleteSourceAccount(athlete_id=a2.id, source="source_a")
    db.add_all([acc1, acc2])
    db.commit()

    assert acc1.id != acc2.id
    assert acc1.athlete_id == a1.id
    assert acc2.athlete_id == a2.id


# ---------------------------------------------------------------------------
# SourceProvider contract
# ---------------------------------------------------------------------------
def test_mock_source_provider_contract(db, athlete):
    account = AthleteSourceAccount(athlete_id=athlete.id, source="source_a")
    db.add(account)
    db.commit()

    provider = MockSourceProvider("source_a")

    auth = provider.authenticate(account)
    assert auth["status"] == "ok"
    assert auth.get("authentication_required") is False

    capabilities = provider.discover_capabilities(account)
    assert any(c.metric == "heart_rate" and c.availability == "native" for c in capabilities)
    assert any(c.metric == "pace" and c.availability == "derived" for c in capabilities)
    assert any(c.metric == "sleep_score" and c.availability == "unavailable" for c in capabilities)

    historical = provider.get_historical_range(account)
    assert historical.start is not None
    assert historical.end is not None

    result = provider.initial_import(account)
    assert result.source == "source_a"
    assert len(result.source_activities) > 0
    assert len(result.activities) > 0
    assert len(result.wellness_records) > 0
    assert len(result.capabilities) > 0

    sync = provider.incremental_sync(account)
    assert sync.source == "source_a"

    raw_bytes = provider.download_activity_file(account, "any-id")
    assert raw_bytes is not None
    assert isinstance(raw_bytes, bytes)

    wellness = provider.get_wellness(account, date(2024, 1, 1), date(2024, 1, 2))
    assert len(wellness) == 2


def test_provider_registry_contains_mock():
    assert SourceProviderRegistry.get("mock") is MockSourceProvider
    assert "mock" in SourceProviderRegistry.list_sources()


# ---------------------------------------------------------------------------
# MetricCapability
# ---------------------------------------------------------------------------
def test_metric_capability_native_derived_estimated_unavailable(db):
    cap = MetricCapability(
        source="source_a",
        metric="heart_rate",
        availability="native",
        confidence_base=0.95,
        notes="Direct sensor measurement",
    )
    db.add(cap)
    db.commit()

    row = db.query(MetricCapability).filter_by(source="source_a", metric="heart_rate").first()
    assert row is cap
    assert row.availability == "native"
    assert float(row.confidence_base) == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# SourcePriority
# ---------------------------------------------------------------------------
def test_source_priority_selects_best_source_for_metric(db, athlete):
    priority = SourcePriority(
        athlete_id=athlete.id,
        metric="hrv",
        priority=["oura", "garmin_fit", "strava"],
    )
    db.add(priority)
    db.commit()

    row = db.query(SourcePriority).filter_by(athlete_id=athlete.id, metric="hrv").first()
    assert row.priority == ["oura", "garmin_fit", "strava"]
    # Verify the engine can read it (sanity import of real dedup logic)
    engine = DedupeEngine()
    priority_map = engine._priority_map(db, athlete.id, "hrv")
    assert priority_map == {"oura": 0, "garmin_fit": 1, "strava": 2}


# ---------------------------------------------------------------------------
# Deduplication engine
# ---------------------------------------------------------------------------
def _make_existing(db, athlete, **kwargs):
    start = kwargs.get("start_time_utc", datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc))
    activity = Activity(
        athlete_id=athlete.id,
        start_time_utc=start,
        sport="running",
        primary_source=kwargs.get("source", "source_a"),
        distance_m=kwargs.get("distance_m", 10000.0),
        duration_elapsed_s=kwargs.get("duration_elapsed_s", 3600.0),
    )
    db.add(activity)
    db.flush()

    sa = SourceActivity(
        id=kwargs.get("id") or uuid.uuid4(),
        athlete_id=athlete.id,
        source=kwargs.get("source", "source_a"),
        source_activity_id=kwargs.get("source_activity_id", "source_a-001"),
        start_time_utc=start,
        sport_raw="running",
        distance_m=kwargs.get("distance_m", 10000.0),
        duration_elapsed_s=kwargs.get("duration_elapsed_s", 3600.0),
        canonical_activity_id=activity.id,
        status="normalized",
        data_quality="authoritative",
    )
    db.add(sa)
    db.commit()
    return sa, activity


def test_dedup_high_score_auto_link(db, athlete):
    existing, _ = _make_existing(db, athlete)
    engine = DedupeEngine()

    new_sa = SourceActivity(
        athlete_id=athlete.id,
        source="source_a",
        source_activity_id="source_a-002",
        start_time_utc=existing.start_time_utc,
        sport_raw="running",
        distance_m=existing.distance_m,
        duration_elapsed_s=existing.duration_elapsed_s,
    )

    action = engine.evaluate(db, new_sa)
    assert action.decision == "AUTO_LINK"
    assert action.existing_source_activity_id == existing.id
    assert action.score is not None
    assert action.score.final_score >= engine.auto_link_threshold
    assert action.score.time_score == 1.0
    assert action.score.distance_score == 1.0
    assert action.score.duration_score == 1.0


def test_dedup_ambiguous_score_review(db, athlete):
    existing, _ = _make_existing(db, athlete)
    priority = SourcePriority(
        athlete_id=athlete.id,
        metric="activity",
        priority=["source_b", "source_a"],
    )
    db.add(priority)
    db.commit()

    engine = DedupeEngine()

    new_sa = SourceActivity(
        athlete_id=athlete.id,
        source="source_b",
        source_activity_id="source_b-001",
        start_time_utc=existing.start_time_utc + timedelta(seconds=250),
        sport_raw="running",
        distance_m=existing.distance_m,
        duration_elapsed_s=existing.duration_elapsed_s,
    )

    action = engine.evaluate(db, new_sa)
    assert action.decision == "REVIEW"
    assert action.review_id is not None
    assert action.score is not None
    assert engine.review_threshold <= action.score.final_score < engine.auto_link_threshold
    assert 0.0 < action.score.time_score < 1.0
    assert action.score.source_bonus == 0.10


def test_dedup_low_score_separate(db, athlete):
    existing, _ = _make_existing(db, athlete)
    engine = DedupeEngine()

    # Same start time but very different distance makes the match implausible.
    new_sa = SourceActivity(
        athlete_id=athlete.id,
        source="source_a",
        source_activity_id="source_a-002",
        start_time_utc=existing.start_time_utc,
        sport_raw="running",
        distance_m=50000.0,
        duration_elapsed_s=existing.duration_elapsed_s,
    )

    action = engine.evaluate(db, new_sa)
    assert action.decision == "SEPARATE"
    assert action.score is not None
    assert action.score.final_score < engine.review_threshold
    assert action.score.time_score == 1.0
    assert action.score.distance_score == 0.0
    assert action.score.duration_score == 1.0


def test_dedup_source_bonus_lowers_score_for_lower_priority(db, athlete):
    existing, _ = _make_existing(db, athlete, source="source_a")
    priority = SourcePriority(
        athlete_id=athlete.id,
        metric="activity",
        priority=["source_a", "source_b"],
    )
    db.add(priority)
    db.commit()

    engine = DedupeEngine()
    new_sa = SourceActivity(
        athlete_id=athlete.id,
        source="source_b",
        source_activity_id="source_b-001",
        start_time_utc=existing.start_time_utc,
        sport_raw="running",
        distance_m=existing.distance_m,
        duration_elapsed_s=existing.duration_elapsed_s,
    )
    action = engine.evaluate(db, new_sa)
    assert action.score is not None
    assert action.score.source_bonus == -0.05


def test_dedup_exact_reimport(db, athlete):
    existing, _ = _make_existing(db, athlete, source="source_a")
    engine = DedupeEngine()

    # Simulated re-import of the exact same source record
    new_sa = SourceActivity(
        athlete_id=athlete.id,
        source="source_a",
        source_activity_id="source_a-001",
        start_time_utc=existing.start_time_utc,
        sport_raw="running",
        distance_m=existing.distance_m,
        duration_elapsed_s=existing.duration_elapsed_s,
    )

    action = engine.evaluate(db, new_sa)
    assert action.decision == "REIMPORT"
    assert action.existing_source_activity_id == existing.id


# ---------------------------------------------------------------------------
# Provenance / data lineage
# ---------------------------------------------------------------------------
def test_activity_provenance(db, athlete):
    lineage = DataLineage(
        source="source_a",
        timestamp=datetime.now(timezone.utc),
        source_record_id="source_a-001",
        raw_file_id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        value_type="native",
        confidence=0.98,
        data_quality="authoritative",
        quality_flags=["sensor_hr"],
    )

    activity = Activity(
        athlete_id=athlete.id,
        start_time_utc=datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
        sport="running",
        primary_source="source_a",
        provenance=lineage.to_dict(),
    )
    db.add(activity)
    db.commit()

    row = db.query(Activity).filter_by(athlete_id=athlete.id).first()
    prov = row.provenance or {}
    assert prov["source"] == "source_a"
    assert prov["value_type"] == "native"
    assert prov["source_record_id"] == "source_a-001"
    assert "raw_file_id" in prov
    assert "device_id" in prov
    assert "account_id" in prov


def test_wellness_provenance(db, athlete):
    lineage = DataLineage(
        source="source_b",
        timestamp=datetime.now(timezone.utc),
        metric="hrv_ms",
        value_type="native",
        confidence=0.91,
    )

    wellness = WellnessDaily(
        athlete_id=athlete.id,
        date=date(2024, 1, 1),
        source="source_b",
        hrv_ms=55.0,
        provenance=lineage.to_dict(),
    )
    db.add(wellness)
    db.commit()

    row = db.query(WellnessDaily).filter_by(athlete_id=athlete.id, source="source_b").first()
    assert row is not None
    assert row.provenance["metric"] == "hrv_ms"
    assert row.provenance["value_type"] == "native"


# ---------------------------------------------------------------------------
# RawFile immutability
# ---------------------------------------------------------------------------
def test_raw_file_immutable_by_sha256(db, tmp_path):
    settings_raw_root = tmp_path / "raw"
    from trailcoach.core.config import settings

    original_root = settings.raw_root
    settings.raw_root = settings_raw_root
    try:
        store = RawStore()
        payload = b"synthetic-fit-content"

        first = store.store(
            db=db,
            content=payload,
            source="source_a",
            kind="activity",
            athlete_id=uuid.uuid4(),
            source_activity_id="source-a-001",
        )

        # Write a second time with the same bytes
        second = store.store(
            db=db,
            content=payload,
            source="source_a",
            kind="activity",
            athlete_id=uuid.uuid4(),
            source_activity_id="source-a-002",
        )

        assert first.id == second.id
        assert first.sha256 == second.sha256

        disk_path = Path(first.storage_path)
        absolute_path = store.root / disk_path
        assert absolute_path.read_bytes() == payload

        # Confirm only one row in the database
        count = db.query(RawFile).filter_by(sha256=first.sha256).count()
        assert count == 1
    finally:
        settings.raw_root = original_root


# ---------------------------------------------------------------------------
# Raw data first sanity check
# ---------------------------------------------------------------------------
def test_raw_store_keeps_original_content(db, tmp_path):
    from trailcoach.core.config import settings

    settings.raw_root = tmp_path / "raw"
    store = RawStore()
    original = b"original-source-bytes"
    raw = store.store(
        db=db,
        content=original,
        source="source_a",
        kind="activity",
        athlete_id=uuid.uuid4(),
    )
    assert store.read_bytes(raw) == original
