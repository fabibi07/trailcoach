"""Cliente HTTP de la API Personal de Garmin Connect (solo lectura).

Usa únicamente la biblioteca estándar. La API key se canjea por un token
JWT de ~24 h que vive solo en memoria; ni la key ni el token se registran
en logs, excepciones ni `repr`. El transporte es inyectable para tests.

Límites verificados en el spike P0.2 (cuenta real, FR955):
- actividades: páginas de hasta 100;
- resúmenes diarios y HRV: hasta 365 días por llamada;
- sueño: hasta 31 días por llamada (60 días → HTTP 400);
- descarga FIT: exige `Accept: */*` (`application/octet-stream` → 406) y
  devuelve un ZIP con el FIT original del reloj.
"""

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

ACTIVITY_PAGE_SIZE = 100
DAILY_SUMMARY_MAX_DAYS = 365
SLEEP_MAX_DAYS = 31
HRV_MAX_DAYS = 365
TOKEN_TTL_S = 23 * 3600

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], timeout: float
) -> HttpResponse:
    """Transporte por defecto basado en `urllib`; los errores HTTP se devuelven, no se lanzan."""
    request = urllib.request.Request(
        url, method=method, headers=headers, data=b"" if method == "POST" else None
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - URL fija
            return HttpResponse(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"", dict(exc.headers.items()))


class GarminConnectError(Exception):
    """Error de la API Personal de Garmin (nunca incluye key ni token)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GarminAuthError(GarminConnectError):
    """La key fue rechazada (HTTP 401/403): vencida o revocada."""


def date_windows(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    """Divide [start, end] en tramos inclusivos de como máximo `max_days` días."""
    current = start
    while current <= end:
        window_end = min(end, current + timedelta(days=max_days - 1))
        yield current, window_end
        current = window_end + timedelta(days=1)


class GarminConnectClient:
    def __init__(
        self,
        api_key: str,
        transport: HttpTransport | None = None,
        base_url: str = BASE_URL,
        pause_s: float = 0.5,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        timeout_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise GarminAuthError("API key de Garmin no configurada")
        self._api_key = api_key
        self._transport: HttpTransport = transport or urllib_transport
        self.base_url = base_url.rstrip("/")
        self.pause_s = pause_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.requests = 0

    def __repr__(self) -> str:
        return f"GarminConnectClient(base_url={self.base_url!r})"

    # ------------------------------------------------------------ HTTP

    def _send(self, method: str, path: str, headers: dict[str, str]) -> HttpResponse:
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            if self.requests and self.pause_s:
                self._sleep(self.pause_s)
            self.requests += 1
            response = self._transport(method, url, headers, self.timeout_s)
            if response.status in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else self.backoff_s * 2**attempt
                self._sleep(delay)
                attempt += 1
                continue
            return response

    def authenticate(self) -> None:
        """Canjea la API key por un token JWT y lo guarda solo en memoria."""
        response = self._send("POST", TOKEN_PATH, {"Authorization": f"ApiKey {self._api_key}"})
        if response.status in (401, 403):
            raise GarminAuthError("API key rechazada por Garmin", response.status)
        if response.status != 200:
            raise GarminConnectError(f"token: HTTP {response.status}", response.status)
        token = self._extract_token(response.body)
        if not token:
            raise GarminConnectError("token: respuesta sin token")
        self._token = token
        self._token_expires_at = self._clock() + TOKEN_TTL_S

    @staticmethod
    def _extract_token(body: bytes) -> str | None:
        text = body.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return text or None
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("token", "accessToken", "access_token", "jwt"):
                if isinstance(data.get(key), str):
                    return str(data[key])
        return None

    def _bearer(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_at:
            self.authenticate()
        assert self._token is not None
        return self._token

    def get(self, path: str, accept: str = "application/json") -> bytes:
        """GET autenticado; ante 401 renueva el token una vez."""
        for refreshed in (False, True):
            response = self._send(
                "GET", path, {"Authorization": f"Bearer {self._bearer()}", "Accept": accept}
            )
            if response.status in (401, 403) and not refreshed:
                self._token = None
                continue
            if response.status in (401, 403):
                raise GarminAuthError(
                    f"{path.split('?')[0]}: HTTP {response.status}", response.status
                )
            if response.status == 204:
                return b""  # Garmin responde 204 cuando el rango no tiene datos
            if response.status != 200:
                raise GarminConnectError(
                    f"{path.split('?')[0]}: HTTP {response.status}", response.status
                )
            return response.body
        raise GarminConnectError(f"{path}: sin respuesta")  # pragma: no cover

    def get_json(self, path: str) -> Any:
        body = self.get(path)
        return json.loads(body) if body.strip() else None

    # -------------------------------------------------------- endpoints

    def list_activities(
        self, start: int = 0, limit: int = ACTIVITY_PAGE_SIZE
    ) -> list[dict[str, Any]]:
        limit = min(limit, ACTIVITY_PAGE_SIZE)
        data = self.get_json(
            f"/activitylist-service/activities/search/activities?start={start}&limit={limit}"
        )
        return list(data or [])

    def iter_activities(self) -> Iterator[dict[str, Any]]:
        """Recorre todas las actividades, de la más reciente a la más antigua."""
        start = 0
        while True:
            page = self.list_activities(start, ACTIVITY_PAGE_SIZE)
            yield from page
            if len(page) < ACTIVITY_PAGE_SIZE:
                return
            start += len(page)

    def download_activity_zip(self, activity_id: str | int) -> bytes:
        return self.get(f"/download-service/files/activity/{activity_id}", accept="*/*")

    def download_fit(self, activity_id: str | int) -> bytes:
        """Devuelve el FIT original contenido en el ZIP de descarga."""
        return extract_fit(self.download_activity_zip(activity_id))

    def daily_summaries(self, start: date, end: date) -> Iterator[bytes]:
        for a, b in date_windows(start, end, DAILY_SUMMARY_MAX_DAYS):
            yield self.get(
                f"/usersummary-service/v2/dailysummaries?startDate={a.isoformat()}"
                f"&endDate={b.isoformat()}"
            )

    def sleep_summaries(self, start: date, end: date) -> Iterator[bytes]:
        for a, b in date_windows(start, end, SLEEP_MAX_DAYS):
            yield self.get(
                f"/sleep-service/sleep/dailySleepSummariesByDate?startDate={a.isoformat()}"
                f"&endDate={b.isoformat()}"
            )

    def hrv_summaries(self, start: date, end: date) -> Iterator[bytes]:
        for a, b in date_windows(start, end, HRV_MAX_DAYS):
            yield self.get(f"/hrv-service/hrv/daily/{a.isoformat()}/{b.isoformat()}")


def extract_fit(content: bytes) -> bytes:
    """Extrae el primer `.fit` de un ZIP; si ya es un FIT lo devuelve tal cual."""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = sorted(n for n in archive.namelist() if n.lower().endswith(".fit"))
        if not names:
            raise GarminConnectError("ZIP de descarga sin archivo .fit")
        return archive.read(names[0])
