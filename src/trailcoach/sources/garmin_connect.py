"""Proveedor de la API Personal de Garmin Connect (`garmin_connect`).

Fuente distinta de `garmin_fit` (export local): la API entrega el mismo FIT
original del reloj, que se parsea con el mismo `FitParser`; la deduplicación
enlaza ambas fuentes a una misma actividad canónica y el `RawStore` comparte
el archivo por SHA-256. Además entrega el bienestar diario en JSON.

Credencial: la API key nunca se guarda en la base de datos. En
`AthleteSourceAccount.auth_json` solo se guarda el nombre de la variable de
entorno (`{"key_env": "GARMIN_PERSONAL_API_KEY"}`); el token JWT vive en
memoria. El proveedor no toca la base de datos: el orquestador persiste RAW,
dedup, bienestar y estado de la cuenta.
"""

import json
import os
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any

from trailcoach.db.models import (
    AthleteSourceAccount,
    MetricCapability,
    WellnessDaily,
    WellnessSourceMetric,
)
from trailcoach.providers.fit_parser import FitParser
from trailcoach.sources.garmin_connect_client import (
    GarminAuthError,
    GarminConnectClient,
    GarminConnectError,
)
from trailcoach.sources.garmin_fit import GARMIN_FIT_CAPABILITIES, fit_payload
from trailcoach.sources.provider import (
    ActivityPayload,
    HistoricalRange,
    ImportResult,
    SourceProvider,
    WellnessPayload,
)
from trailcoach.sources.registry import SourceProviderRegistry

DEFAULT_KEY_ENV = "GARMIN_PERSONAL_API_KEY"
ACTIVITY_CURSOR = "last_start_time_utc"
WELLNESS_CURSOR = "wellness_through"
DAY_IN_PROGRESS = "day_in_progress"

_WELLNESS_CAPABILITIES: list[tuple[str, str, str | None]] = [
    ("resting_hr", "native", "Resumen diario: heartRate.restingValue"),
    ("hrv", "native", "HRV nocturna: hrvSummaries.lastNightAvg/status"),
    ("sleep_score", "native", "Resumen de sueño: sleepScores.overall.value"),
    ("body_battery", "native", "Resumen diario: bodyBattery min/max/cargado/drenado"),
    ("stress", "native", "Resumen diario: stress.avgLevel/maxLevel"),
    ("steps", "native", "Resumen diario: movement.steps.value"),
    ("respiration", "native", "Resumen diario y sueño"),
    ("training_readiness", "unavailable", "No expuesto por la API Personal (HTTP 404)"),
    ("vo2max_history", "unavailable", "No expuesto por la API Personal (HTTP 404)"),
    ("training_status", "unavailable", "No expuesto por la API Personal (HTTP 404)"),
]
_FIT_WELLNESS = {metric for metric, _, _ in _WELLNESS_CAPABILITIES}
GARMIN_CONNECT_CAPABILITIES = [
    cap for cap in GARMIN_FIT_CAPABILITIES if cap[0] not in _FIT_WELLNESS
] + _WELLNESS_CAPABILITIES

MetricSpec = tuple[str, tuple[str, ...], str | None]

DAILY_METRICS: list[MetricSpec] = [
    ("steps", ("movement", "steps", "value"), "count"),
    ("floors_ascended", ("movement", "floorsAscended", "value"), "count"),
    ("hr_min_bpm", ("heartRate", "minValue"), "bpm"),
    ("hr_max_bpm", ("heartRate", "maxValue"), "bpm"),
    ("stress_avg", ("stress", "avgLevel"), "score"),
    ("stress_max", ("stress", "maxLevel"), "score"),
    ("body_battery_charged", ("bodyBattery", "chargedValue"), "score"),
    ("body_battery_drained", ("bodyBattery", "drainedValue"), "score"),
    ("intensity_min_moderate", ("intensityMinutes", "moderate"), "min"),
    ("intensity_min_vigorous", ("intensityMinutes", "vigorous"), "min"),
    ("respiration_avg_brpm", ("respiration", "avgValue"), "brpm"),
    ("calories_total_kcal", ("calories", "burnedTotal"), "kcal"),
    ("calories_active_kcal", ("calories", "burnedActive"), "kcal"),
]
SLEEP_METRICS: list[MetricSpec] = [
    ("sleep_deep_s", ("deepSleepSeconds",), "s"),
    ("sleep_light_s", ("lightSleepSeconds",), "s"),
    ("sleep_rem_s", ("remSleepSeconds",), "s"),
    ("sleep_awake_s", ("awakeSleepSeconds",), "s"),
    ("sleep_nap_s", ("napTimeSeconds",), "s"),
    ("sleep_avg_hr_bpm", ("avgHeartRate",), "bpm"),
    ("sleep_avg_respiration_brpm", ("averageRespirationValue",), "brpm"),
    ("sleep_avg_stress", ("avgSleepStress",), "score"),
    ("sleep_need_min", ("sleepNeed", "actual"), "min"),
]
HRV_METRICS: list[MetricSpec] = [
    ("hrv_weekly_avg_ms", ("weeklyAvg",), "ms"),
    ("hrv_last_night_5min_high_ms", ("lastNight5MinHigh",), "ms"),
    ("hrv_baseline_low_ms", ("baseline", "balancedLow"), "ms"),
    ("hrv_baseline_high_ms", ("baseline", "balancedUpper"), "ms"),
]


def _dig(data: Any, path: Iterable[str]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(round(number)) if number is not None else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_gmt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


class WellnessMapper:
    """Convierte respuestas JSON de bienestar en filas canónicas (función pura)."""

    def __init__(self, source: str, athlete_id: Any, today: date) -> None:
        self.source = source
        self.athlete_id = athlete_id
        self.today = today

    def _record(self, day: date, **fields: Any) -> WellnessDaily:
        partial = day >= self.today
        return WellnessDaily(
            athlete_id=self.athlete_id,
            date=day,
            source=self.source,
            data_quality="partial" if partial else "authoritative",
            quality_flags=[DAY_IN_PROGRESS] if partial else [],
            provenance={"source": self.source},
            **fields,
        )

    def _metrics(
        self, day: date, item: dict[str, Any], specs: list[MetricSpec]
    ) -> list[WellnessSourceMetric]:
        out = []
        for metric, path, unit in specs:
            value = _number(_dig(item, path))
            if value is None:
                continue
            out.append(
                WellnessSourceMetric(
                    athlete_id=self.athlete_id,
                    date=day,
                    source=self.source,
                    metric=metric,
                    value=value,
                    unit=unit,
                    value_type="native",
                    source_field=".".join(path)[:64],
                )
            )
        return out

    def _payload(
        self,
        name: str,
        body: bytes,
        items: list[dict[str, Any]],
        build: Callable[[date, dict[str, Any]], WellnessDaily | None],
        specs: list[MetricSpec],
    ) -> WellnessPayload | None:
        payload = WellnessPayload(name=name, raw_content=body)
        for item in items:
            day = _parse_date(item.get("calendarDate"))
            if day is None:
                continue
            record = build(day, item)
            metrics = self._metrics(day, item, specs)
            if record is not None:
                payload.records.append(record)
            payload.metrics.extend(metrics)
        if not payload.records and not payload.metrics:
            return None
        days = [r.date for r in payload.records] + [m.date for m in payload.metrics]
        payload.captured_at = min(days)
        return payload

    def daily_summaries(self, body: bytes) -> WellnessPayload | None:
        data = json.loads(body) if body.strip() else {}
        items = data.get("data", []) if isinstance(data, dict) else list(data or [])

        def build(day: date, item: dict[str, Any]) -> WellnessDaily | None:
            fields = {
                "resting_hr": _number(_dig(item, ("heartRate", "restingValue"))),
                "body_battery_min": _int(_dig(item, ("bodyBattery", "minValue"))),
                "body_battery_max": _int(_dig(item, ("bodyBattery", "maxValue"))),
            }
            if all(v is None for v in fields.values()):
                return None
            return self._record(day, **fields)

        return self._payload("daily_summary", body, items, build, DAILY_METRICS)

    def sleep_summaries(self, body: bytes) -> WellnessPayload | None:
        data = json.loads(body) if body.strip() else []
        items = list(data) if isinstance(data, list) else []

        def build(day: date, item: dict[str, Any]) -> WellnessDaily | None:
            duration = _number(item.get("sleepTimeSeconds"))
            score = _int(_dig(item, ("sleepScores", "overall", "value")))
            if not duration and score is None:
                return None
            return self._record(day, sleep_duration_s=duration or None, sleep_score=score)

        items = [i for i in items if _number(i.get("sleepTimeSeconds"))]
        return self._payload("sleep", body, items, build, SLEEP_METRICS)

    def hrv_summaries(self, body: bytes) -> WellnessPayload | None:
        data = json.loads(body) if body.strip() else {}
        items = data.get("hrvSummaries", []) if isinstance(data, dict) else []

        def build(day: date, item: dict[str, Any]) -> WellnessDaily | None:
            hrv = _number(item.get("lastNightAvg"))
            status = item.get("status")
            if hrv is None and not status:
                return None
            return self._record(day, hrv_ms=hrv, hrv_status=str(status)[:16] if status else None)

        return self._payload("hrv", body, items, build, HRV_METRICS)


ClientFactory = Callable[[str], GarminConnectClient]


@SourceProviderRegistry.register
class GarminConnectProvider(SourceProvider):
    source = "garmin_connect"

    def __init__(
        self,
        api_key: str | None = None,
        client_factory: ClientFactory | None = None,
        include_wellness: bool = True,
        refetch_days: int = 3,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._api_key = api_key
        self._client_factory: ClientFactory = client_factory or GarminConnectClient
        self.include_wellness = include_wellness
        self.refetch_days = refetch_days
        self._today = today
        self._client: GarminConnectClient | None = None
        self.parser = FitParser(source=self.source)

    def __repr__(self) -> str:
        return "GarminConnectProvider()"

    # ------------------------------------------------------------ helpers

    @staticmethod
    def key_env(account: AthleteSourceAccount) -> str:
        return str((account.auth_json or {}).get("key_env") or DEFAULT_KEY_ENV)

    def _resolve_key(self, account: AthleteSourceAccount) -> str | None:
        return self._api_key or os.environ.get(self.key_env(account)) or None

    def _get_client(self, account: AthleteSourceAccount) -> GarminConnectClient:
        if self._client is None:
            key = self._resolve_key(account)
            if not key:
                raise GarminAuthError(f"variable {self.key_env(account)} no configurada")
            self._client = self._client_factory(key)
        return self._client

    def _list_activities(
        self, client: GarminConnectClient, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        items = []
        for item in client.iter_activities():
            started = _parse_gmt(item.get("startTimeGMT"))
            if since is not None and started is not None and started <= since:
                break
            items.append(item)
        return items

    def _import_activities(
        self,
        account: AthleteSourceAccount,
        client: GarminConnectClient,
        items: list[dict[str, Any]],
        result: ImportResult,
    ) -> None:
        last_start: datetime | None = None
        for item in sorted(items, key=lambda i: str(i.get("startTimeGMT") or "")):
            sa_id = str(item.get("activityId") or "")
            if not sa_id:
                result.errors.append("actividad sin activityId; omitida")
                continue
            try:
                content = client.download_fit(sa_id)
                doc = self.parser.parse_bytes(content)
            except GarminAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 - un FIT malo no aborta el lote
                result.errors.append(f"{sa_id}: {exc}")
                continue
            if doc.start_time is None:
                result.errors.append(f"{sa_id}: FIT sin hora de inicio; omitida")
                continue
            sa = self.parser.build_source_activity(
                doc, account.athlete_id, sa_id, account_id=account.id
            )
            sa.provenance = {
                **(sa.provenance or {}),
                "platform": "garmin_connect",
                "activity_type": _dig(item, ("activityType", "typeKey")),
                "fit_natural_id": doc.natural_id(),
            }
            result.source_activities.append(sa)
            result.activities.append(self.parser.build_activity(doc, account.athlete_id))
            result.payloads[sa_id] = fit_payload(self.parser, doc, content, sa_id)
            if last_start is None or doc.start_time > last_start:
                last_start = doc.start_time
        if last_start is not None:
            result.cursor[ACTIVITY_CURSOR] = last_start.isoformat()

    def wellness_payloads(
        self, account: AthleteSourceAccount, start: date, end: date
    ) -> list[WellnessPayload]:
        """Descarga y mapea resumen diario, sueño y HRV de [start, end]."""
        client = self._get_client(account)
        mapper = WellnessMapper(self.source, account.athlete_id, self._today())
        payloads: list[WellnessPayload | None] = []
        payloads += [mapper.daily_summaries(b) for b in client.daily_summaries(start, end)]
        payloads += [mapper.sleep_summaries(b) for b in client.sleep_summaries(start, end)]
        payloads += [mapper.hrv_summaries(b) for b in client.hrv_summaries(start, end)]
        return [p for p in payloads if p is not None]

    def _import_wellness(
        self, account: AthleteSourceAccount, start: date, end: date, result: ImportResult
    ) -> None:
        if not self.include_wellness or start > end:
            return
        try:
            result.wellness_payloads.extend(self.wellness_payloads(account, start, end))
        except GarminAuthError:
            raise
        except GarminConnectError as exc:
            result.errors.append(f"bienestar: {exc}")
            return
        result.cursor[WELLNESS_CURSOR] = end.isoformat()

    def _run(
        self,
        account: AthleteSourceAccount,
        result: ImportResult,
        activity_filter: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        since: datetime | None,
        wellness_range: Callable[[list[dict[str, Any]]], tuple[date, date]],
    ) -> ImportResult:
        result.capabilities = self.discover_capabilities(account)
        try:
            client = self._get_client(account)
            listed = self._list_activities(client, since)
            self._import_activities(account, client, activity_filter(listed), result)
            self._import_wellness(account, *wellness_range(listed), result)
        except GarminConnectError as exc:
            result.errors.append(str(exc))
        return result

    # ----------------------------------------------------------- contract

    def authenticate(self, account: AthleteSourceAccount) -> dict[str, Any]:
        try:
            self._get_client(account).authenticate()
        except GarminAuthError as exc:
            missing = self._resolve_key(account) is None
            return {
                "status": "error",
                "auth_status": "missing_api_key" if missing else "expired_or_revoked",
                "error": str(exc),
            }
        except GarminConnectError as exc:
            return {"status": "error", "auth_status": "unreachable", "error": str(exc)}
        return {
            "status": "ok",
            "auth_status": "ok",
            "source_type": "personal_api",
            "key_env": self.key_env(account),
        }

    def discover_capabilities(self, account: AthleteSourceAccount) -> list[MetricCapability]:
        return [
            MetricCapability(
                source=self.source,
                metric=metric,
                availability=availability,
                confidence_base=0.95 if availability == "native" else None,
                notes=notes,
            )
            for metric, availability, notes in GARMIN_CONNECT_CAPABILITIES
        ]

    def get_historical_range(self, account: AthleteSourceAccount) -> HistoricalRange:
        starts = [
            d
            for d in (
                _parse_date(i.get("startTimeLocal"))
                for i in self._list_activities(self._get_client(account))
            )
            if d is not None
        ]
        if not starts:
            return HistoricalRange()
        return HistoricalRange(start=min(starts), end=max(starts))

    def initial_import(
        self,
        account: AthleteSourceAccount,
        start: date | None = None,
        end: date | None = None,
    ) -> ImportResult:
        today = self._today()

        def in_range(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for item in items:
                day = _parse_date(item.get("startTimeLocal"))
                if day is None or (start and day < start) or (end and day > end):
                    continue
                out.append(item)
            return out

        def wellness_range(items: list[dict[str, Any]]) -> tuple[date, date]:
            days = [d for d in (_parse_date(i.get("startTimeLocal")) for i in items) if d]
            first = start or (min(days) if days else today)
            return first, min(end or today, today)

        result = ImportResult(source=self.source, start=start, end=end)
        return self._run(account, result, in_range, None, wellness_range)

    def incremental_sync(self, account: AthleteSourceAccount) -> ImportResult:
        cursor = account.cursor_json or {}
        since = _parse_gmt(cursor[ACTIVITY_CURSOR]) if cursor.get(ACTIVITY_CURSOR) else None
        if since is not None:
            since = since.replace(tzinfo=None)
        today = self._today()
        through = _parse_date(cursor.get(WELLNESS_CURSOR)) if cursor.get(WELLNESS_CURSOR) else None
        wellness_start = (through or today) - timedelta(days=self.refetch_days)

        result = ImportResult(source=self.source)
        self._run(account, result, list, since, lambda _: (wellness_start, today))
        if ACTIVITY_CURSOR not in result.cursor and cursor.get(ACTIVITY_CURSOR):
            result.cursor[ACTIVITY_CURSOR] = cursor[ACTIVITY_CURSOR]
        return result

    def download_activity_file(
        self, account: AthleteSourceAccount, source_activity_id: str
    ) -> bytes | None:
        try:
            return self._get_client(account).download_fit(source_activity_id)
        except GarminConnectError:
            return None

    def payload_from_raw(self, content: bytes, source_activity_id: str) -> ActivityPayload | None:
        return fit_payload(
            self.parser, self.parser.parse_bytes(content), content, source_activity_id
        )

    def get_wellness(
        self, account: AthleteSourceAccount, start: date, end: date
    ) -> list[WellnessDaily]:
        return [r for p in self.wellness_payloads(account, start, end) for r in p.records]


__all__ = ["GarminConnectProvider", "WellnessMapper"]
