"""Tests P0.2 de GarminConnectProvider.

Todo es sintético: los FIT vienen de `tests/fit_builder.py` y la API de
Garmin se simula con un transporte HTTP falso. No hay red ni credenciales.
"""

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner
from fit_builder import SyntheticActivity, build_fit

from trailcoach.db.models import (
    ActivityLink,
    Athlete,
    AthleteSourceAccount,
    RawFile,
    SourceActivity,
    WellnessDaily,
    WellnessSourceMetric,
)
from trailcoach.ingest.orchestrator import IngestionOrchestrator
from trailcoach.ingest.reprocess import reprocess_activity_metrics
from trailcoach.sources import GarminConnectProvider, GarminFitProvider, SourceProviderRegistry
from trailcoach.sources.garmin_connect import WellnessMapper
from trailcoach.sources.garmin_connect_client import (
    SLEEP_MAX_DAYS,
    TOKEN_PATH,
    GarminAuthError,
    GarminConnectClient,
    HttpResponse,
    date_windows,
)

API_KEY = "clave-sintetica-123"
TOKEN = "jwt-sintetico-456"
TODAY = date(2024, 3, 12)
START = datetime(2024, 3, 10, 7, 30, tzinfo=timezone.utc)


def zip_fit(content: bytes, name: str = "1_ACTIVITY.fit") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, content)
    return buf.getvalue()


def daily_item(day: date, resting: int = 50) -> dict:
    return {
        "calendarDate": day.isoformat(),
        "heartRate": {"minValue": 45, "maxValue": 150, "restingValue": resting},
        "bodyBattery": {"minValue": 20, "maxValue": 90, "chargedValue": 60, "drainedValue": 55},
        "movement": {"steps": {"value": 8000}},
        "stress": {"avgLevel": 25, "maxLevel": 90},
        "respiration": {"avgValue": 14},
    }


def sleep_item(day: date) -> dict:
    return {
        "calendarDate": day.isoformat(),
        "sleepTimeSeconds": 25200,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 14400,
        "remSleepSeconds": 4800,
        "awakeSleepSeconds": 600,
        "sleepScores": {"overall": {"value": 81}},
        "sleepNeed": {"actual": 480},
    }


def hrv_item(day: date) -> dict:
    return {
        "calendarDate": day.isoformat(),
        "lastNightAvg": 42,
        "weeklyAvg": 40,
        "status": "BALANCED",
        "baseline": {"balancedLow": 35, "balancedUpper": 49},
    }


class FakeGarmin:
    """Transporte HTTP falso con las reglas observadas en la API real."""

    def __init__(self) -> None:
        self.activities: list[dict] = []
        self.fits: dict[str, bytes] = {}
        self.days: list[date] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.revoked = False
        self.throttle = 0

    def add_activity(self, activity_id: int, spec: SyntheticActivity) -> bytes:
        content = build_fit(spec)
        stamp = spec.start.strftime("%Y-%m-%d %H:%M:%S")
        self.activities.insert(
            0,
            {
                "activityId": activity_id,
                "startTimeGMT": stamp,
                "startTimeLocal": stamp,
                "activityType": {"typeKey": "running"},
            },
        )
        self.activities.sort(key=lambda a: a["startTimeGMT"], reverse=True)
        self.fits[str(activity_id)] = content
        return content

    def _in(self, start: str, end: str) -> list[date]:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        return [d for d in self.days if a <= d <= b]

    def __call__(self, method, url, headers, timeout) -> HttpResponse:
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.calls.append((method, path, dict(headers)))
        if path == TOKEN_PATH:
            if self.revoked or headers.get("Authorization") != f"ApiKey {API_KEY}":
                return HttpResponse(401, b"")
            return HttpResponse(200, TOKEN.encode())
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return HttpResponse(401, b"")
        if self.throttle:
            self.throttle -= 1
            return HttpResponse(429, b"", {"Retry-After": "1"})
        if path.endswith("/search/activities"):
            start, limit = int(query["start"][0]), int(query["limit"][0])
            return self._json(self.activities[start : start + limit])
        if path.startswith("/download-service/files/activity/"):
            if headers.get("Accept") != "*/*":
                return HttpResponse(406, b"")
            fit = self.fits.get(path.rsplit("/", 1)[1])
            return HttpResponse(200, zip_fit(fit)) if fit else HttpResponse(404, b"")
        if path.startswith("/usersummary-service/"):
            days = self._in(query["startDate"][0], query["endDate"][0])
            return self._json({"data": [daily_item(d) for d in days]})
        if path.startswith("/sleep-service/"):
            a, b = query["startDate"][0], query["endDate"][0]
            if (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 > SLEEP_MAX_DAYS:
                return HttpResponse(400, b"")
            return self._json([sleep_item(d) for d in self._in(a, b)])
        if path.startswith("/hrv-service/"):
            a, b = path.rsplit("/", 2)[1:]
            if not self._in(a, b):
                return HttpResponse(204, b"")
            return self._json({"hrvSummaries": [hrv_item(d) for d in self._in(a, b)]})
        return HttpResponse(404, b"")

    @staticmethod
    def _json(data) -> HttpResponse:
        return HttpResponse(200, json.dumps(data).encode())


def make_client(fake: FakeGarmin, key: str = API_KEY) -> GarminConnectClient:
    return GarminConnectClient(key, transport=fake, pause_s=0, sleep=lambda _s: None)


def make_provider(fake: FakeGarmin, **kwargs) -> GarminConnectProvider:
    return GarminConnectProvider(
        api_key=kwargs.pop("api_key", API_KEY),
        client_factory=lambda key: make_client(fake, key),
        today=lambda: TODAY,
        **kwargs,
    )


@pytest.fixture
def fake() -> FakeGarmin:
    f = FakeGarmin()
    f.add_activity(1001, SyntheticActivity(start=START))
    f.add_activity(1002, SyntheticActivity(start=START + timedelta(days=1), serial_number=1))
    f.days = [date(2024, 3, 10), date(2024, 3, 11), TODAY]
    return f


@pytest.fixture
def athlete(db):
    a = Athlete(display_name="Atleta Garmin Connect")
    db.add(a)
    db.flush()
    return a


@pytest.fixture
def account(db, athlete):
    acc = AthleteSourceAccount(
        athlete_id=athlete.id,
        source="garmin_connect",
        auth_json={"key_env": "GARMIN_PERSONAL_API_KEY"},
    )
    db.add(acc)
    db.flush()
    return acc


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------
def test_date_windows_respect_sleep_limit():
    windows = list(date_windows(date(2024, 1, 1), date(2024, 12, 31), SLEEP_MAX_DAYS))
    assert windows[0] == (date(2024, 1, 1), date(2024, 1, 31))
    assert windows[-1][1] == date(2024, 12, 31)
    assert all((b - a).days + 1 <= SLEEP_MAX_DAYS for a, b in windows)
    assert sum((b - a).days + 1 for a, b in windows) == 366


def test_client_fetches_sleep_in_31_day_windows(fake):
    fake.days = [date(2024, 1, 1) + timedelta(days=i) for i in range(90)]
    bodies = list(make_client(fake).sleep_summaries(date(2024, 1, 1), date(2024, 3, 30)))
    assert len(bodies) == 3
    assert sum(len(json.loads(b)) for b in bodies) == 90


def test_empty_range_with_http_204_is_not_an_error(db, account, fake):
    provider = make_provider(fake)
    result = provider.initial_import(account, start=date(2023, 1, 1), end=TODAY)
    assert result.errors == []
    assert {p.name for p in result.wellness_payloads} == {"daily_summary", "sleep", "hrv"}
    assert result.cursor["wellness_through"] == TODAY.isoformat()


def test_client_downloads_fit_with_accept_any(fake):
    content = make_client(fake).download_fit(1001)
    assert content == fake.fits["1001"]
    download = [c for c in fake.calls if c[1].startswith("/download-service/")][0]
    assert download[2]["Accept"] == "*/*"


def test_client_retries_429_with_backoff(fake):
    fake.throttle = 2
    sleeps: list[float] = []
    client = GarminConnectClient(API_KEY, transport=fake, pause_s=0, sleep=sleeps.append)
    assert len(client.list_activities()) == 2
    assert sleeps == [1.0, 1.0]


def test_client_never_exposes_key_or_token(fake):
    client = make_client(fake)
    client.authenticate()
    assert API_KEY not in repr(client) and TOKEN not in repr(client)
    fake.revoked = True
    client._token = None
    with pytest.raises(GarminAuthError) as exc:
        client.authenticate()
    assert API_KEY not in str(exc.value) and TOKEN not in str(exc.value)


# ---------------------------------------------------------------------------
# Mapeo de bienestar (función pura)
# ---------------------------------------------------------------------------
def test_wellness_mapper_maps_canonical_fields_and_metrics():
    mapper = WellnessMapper("garmin_connect", None, TODAY)
    day = date(2024, 3, 10)
    daily = mapper.daily_summaries(json.dumps({"data": [daily_item(day)]}).encode())
    sleep = mapper.sleep_summaries(json.dumps([sleep_item(day)]).encode())
    hrv = mapper.hrv_summaries(json.dumps({"hrvSummaries": [hrv_item(day)]}).encode())
    assert daily and sleep and hrv
    d, s, h = daily.records[0], sleep.records[0], hrv.records[0]
    assert (d.resting_hr, d.body_battery_min, d.body_battery_max) == (50, 20, 90)
    assert (s.sleep_score, s.sleep_duration_s) == (81, 25200)
    assert (h.hrv_ms, h.hrv_status) == (42, "BALANCED")
    assert d.data_quality == "authoritative"
    metrics = {m.metric: m.value for p in (daily, sleep, hrv) for m in p.metrics}
    assert metrics["steps"] == 8000
    assert metrics["stress_avg"] == 25
    assert metrics["sleep_deep_s"] == 5400
    assert metrics["sleep_need_min"] == 480
    assert metrics["hrv_baseline_low_ms"] == 35
    assert daily.raw_content is not None


def test_wellness_mapper_marks_today_partial_and_skips_empty():
    mapper = WellnessMapper("garmin_connect", None, TODAY)
    today = mapper.daily_summaries(json.dumps({"data": [daily_item(TODAY)]}).encode())
    assert today is not None
    assert today.records[0].data_quality == "partial"
    assert today.records[0].quality_flags == ["day_in_progress"]
    assert mapper.sleep_summaries(b"[]") is None
    assert (
        mapper.daily_summaries(json.dumps({"data": [{"calendarDate": "2024-03-01"}]}).encode())
        is None
    )


# ---------------------------------------------------------------------------
# Proveedor + orquestador
# ---------------------------------------------------------------------------
def test_provider_is_registered_and_distinct_from_fit():
    assert SourceProviderRegistry.get("garmin_connect") is GarminConnectProvider
    assert GarminConnectProvider.source != GarminFitProvider.source


def test_initial_import_activities_wellness_and_raw(db, account, fake):
    summary = IngestionOrchestrator(db).run(account, make_provider(fake))
    assert summary.errors == []
    assert summary.created == 2 and summary.failed == 0
    assert summary.wellness == 3
    assert account.health == "ok" and account.auth_status == "ok"
    assert account.auth_json == {"key_env": "GARMIN_PERSONAL_API_KEY"}
    assert API_KEY not in json.dumps(account.auth_json) + json.dumps(account.cursor_json)
    assert TOKEN not in json.dumps(account.cursor_json)

    sas = db.query(SourceActivity).filter_by(source="garmin_connect").all()
    assert sorted(sa.source_activity_id for sa in sas) == ["1001", "1002"]
    assert all(sa.fit_raw_file_id for sa in sas)

    rows = (
        db.query(WellnessDaily)
        .filter_by(source="garmin_connect")
        .order_by(WellnessDaily.date)
        .all()
    )
    assert len(rows) == 3
    first = rows[0]
    assert (first.resting_hr, first.sleep_score, first.hrv_ms) == (50, 81, 42)
    assert set(first.provenance["raw_file_ids"]) == {"daily_summary", "sleep", "hrv"}
    assert first.raw_file_id is not None
    assert rows[-1].data_quality == "partial"

    metrics = db.query(WellnessSourceMetric).filter_by(date=date(2024, 3, 10)).all()
    assert {m.metric for m in metrics} >= {
        "steps",
        "stress_max",
        "sleep_rem_s",
        "hrv_weekly_avg_ms",
    }
    assert all(m.raw_file_id for m in metrics)
    raw_kinds = {r.kind for r in db.query(RawFile).all()}
    assert raw_kinds == {"fit", "wellness_json"}
    assert account.cursor_json["wellness_through"] == TODAY.isoformat()


def test_second_run_reimports_without_duplicating_wellness(db, account, fake):
    IngestionOrchestrator(db).run(account, make_provider(fake))
    n_wellness = db.query(WellnessDaily).count()
    n_metrics = db.query(WellnessSourceMetric).count()
    n_raw = db.query(RawFile).count()
    summary = IngestionOrchestrator(db).run(account, make_provider(fake))
    assert summary.reimported == 2 and summary.created == 0
    assert db.query(WellnessDaily).count() == n_wellness
    assert db.query(WellnessSourceMetric).count() == n_metrics
    assert db.query(RawFile).count() == n_raw


def test_partial_record_does_not_erase_existing_values(db, account, fake):
    orch = IngestionOrchestrator(db)
    orch.run(account, make_provider(fake))
    fake.days = []
    fake.activities = []
    mapper = WellnessMapper("garmin_connect", account.athlete_id, TODAY)
    payload = mapper.hrv_summaries(
        json.dumps({"hrvSummaries": [hrv_item(date(2024, 3, 10)) | {"lastNightAvg": 44}]}).encode()
    )
    assert payload is not None
    orch._ingest_wellness_payload(account, payload)
    row = db.get(WellnessDaily, (account.athlete_id, date(2024, 3, 10), "garmin_connect"))
    assert row is not None
    assert (row.hrv_ms, row.resting_hr, row.sleep_score) == (44, 50, 81)


def test_revoked_key_marks_account_expired(db, account, fake):
    fake.revoked = True
    summary = IngestionOrchestrator(db).run(account, make_provider(fake))
    assert account.auth_status == "expired_or_revoked"
    assert account.health == "error"
    assert summary.created == 0
    assert all(API_KEY not in e and TOKEN not in e for e in summary.errors)


def test_missing_key_reports_missing_api_key(db, account, fake, monkeypatch):
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    IngestionOrchestrator(db).run(account, make_provider(fake, api_key=None))
    assert account.auth_status == "missing_api_key"
    assert account.health == "error"


def test_key_is_read_from_env_var_named_in_account(db, account, fake, monkeypatch):
    account.auth_json = {"key_env": "OTRA_VARIABLE"}
    monkeypatch.setenv("OTRA_VARIABLE", API_KEY)
    summary = IngestionOrchestrator(db).run(account, make_provider(fake, api_key=None))
    assert summary.created == 2


def test_incremental_sync_only_new_activities(db, account, fake):
    IngestionOrchestrator(db).run(account, make_provider(fake))
    fake.add_activity(1003, SyntheticActivity(start=START + timedelta(days=2), serial_number=2))
    summary = IngestionOrchestrator(db).run(account, make_provider(fake), mode="incremental")
    assert summary.created == 1 and summary.reimported == 0
    assert db.query(SourceActivity).filter_by(source="garmin_connect").count() == 3


def test_no_wellness_flag(db, account, fake):
    summary = IngestionOrchestrator(db).run(account, make_provider(fake, include_wellness=False))
    assert summary.created == 2 and summary.wellness == 0
    assert db.query(WellnessDaily).count() == 0


def test_auto_links_against_garmin_fit_and_shares_raw(db, athlete, account, fake, tmp_path: Path):
    fit_dir = tmp_path / "export"
    fit_dir.mkdir()
    (fit_dir / "a.fit").write_bytes(fake.fits["1001"])
    fit_account = AthleteSourceAccount(
        athlete_id=athlete.id, source="garmin_fit", auth_json={"import_dir": str(fit_dir)}
    )
    db.add(fit_account)
    db.flush()
    IngestionOrchestrator(db).run(fit_account, GarminFitProvider())

    summary = IngestionOrchestrator(db).run(account, make_provider(fake))
    assert summary.linked == 1 and summary.created == 1
    fit_sa = db.query(SourceActivity).filter_by(source="garmin_fit").one()
    api_sa = (
        db.query(SourceActivity).filter_by(source="garmin_connect", source_activity_id="1001").one()
    )
    assert api_sa.canonical_activity_id == fit_sa.canonical_activity_id
    assert api_sa.fit_raw_file_id == fit_sa.fit_raw_file_id
    link = db.query(ActivityLink).filter_by(source_activity_id=api_sa.id).one()
    assert link.match_method == "dedupe_auto"


def test_reprocess_raw_rebuilds_from_raw(db, account, fake):
    IngestionOrchestrator(db).run(account, make_provider(fake))
    summary = reprocess_activity_metrics(db, GarminConnectProvider(), account.athlete_id)
    assert summary.processed == 2 and summary.failed == 0


def test_cli_ingest_garmin_connect(db, athlete, fake, monkeypatch):
    from trailcoach.cli import main as cli_main

    monkeypatch.setattr(cli_main, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    class PatchedProvider(GarminConnectProvider):
        def __init__(self, api_key=None, include_wellness=True):
            super().__init__(
                api_key=API_KEY,
                client_factory=lambda key: make_client(fake, key),
                include_wellness=include_wellness,
                today=lambda: TODAY,
            )

    monkeypatch.setattr(cli_main, "GarminConnectProvider", PatchedProvider)
    result = CliRunner().invoke(
        cli_main.cli, ["ingest-garmin-connect", "--athlete-id", str(athlete.id), "--no-wellness"]
    )
    assert result.exit_code == 0, result.output
    assert "created=2" in result.output and "wellness_days=0" in result.output
    assert API_KEY not in result.output
    acc = db.query(AthleteSourceAccount).filter_by(source="garmin_connect").one()
    assert acc.auth_json == {"key_env": "GARMIN_PERSONAL_API_KEY"}
