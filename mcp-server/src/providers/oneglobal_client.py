"""Cliente de la API de 1GLOBAL (Truphone IoT Portal) con modo replay.

Implementado según `guide_api_global.md`:

- Autenticación ``Authorization: <scheme> <token>`` (esquema configurable,
  'Token' por defecto; NO se sustituye por 'Bearer' automáticamente).
- SMS: rutas v2.0 (v2.2 es solo catálogo de SIM/Devices).
- ``POST /v2.0/sims/send_sms/``: UN ICCID por petición, texto EXACTO sin
  recortar ni normalizar.
- ``GET /v2.0/sims/send_sms/status/{external_task_id}/``: estado de entrega.
  Nunca se consulta con el id local.
- ``GET /v2.0/sims/send_sms/status/?dateSubmitted_from=…&iccid=…&ordering=-dateSubmitted``:
  detalles de SMS para correlacionar la respuesta del equipo (MO).
- Parser tolerante: ``id``/``sms_id``/``taskId``, ``resourceURL`` (incluida la
  doble barra real ``send_sms//status/{id}``), envoltorios ``{data}``,
  ``{results}``, ``{deliveryReport}`` o array plano.
- Fechas del filtro: 'YYYY-MM-DD HH:mm:ss' (sin 'T', sin 'Z', sin ms).

POST con efectos secundarios: NUNCA se reintenta automáticamente; un timeout
o 504 dudoso se reporta como `UncertainSubmission` para conciliación manual.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from src.utils.logging_utils import redact
from src.utils.rate_limiter import AsyncTokenLimiter
from src.utils.retry import backoff_delay, is_retryable_status
from src.utils.time_utils import format_api_date, parse_api_date


class ProviderError(Exception):
    """Error de la API de 1GLOBAL (sin ambigüedad: la llamada no pudo cursarse)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class UncertainSubmission(Exception):
    """POST dudoso: 1GLOBAL pudo haber ACEPTADO el envío sin responder.

    No se repite automáticamente (no se conocen semánticas de idempotencia
    del proveedor). La tarea queda 'uncertain' y se concilia manualmente.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class SendSubmitResult:
    """Resultado de una sumisión de SMS."""

    external_task_id: str | None
    raw: dict[str, Any]
    accepted: bool


@dataclass
class ProviderStatus:
    """Estado de entrega de un envío ya confirmado."""

    raw_status: str | None
    status: str  # normalizado: pending|sent|delivered|failed|cancelled|unknown
    content: str | None
    date_submitted: str | None
    raw: dict[str, Any]


@dataclass
class SmsDetail:
    """Un reporte del listado de detalles de SMS (puede incluir el eco saliente)."""

    iccid: str | None
    content: str | None
    date_submitted: str | None
    raw_status: str | None


# --------------------------------------------------------------------------- #
# Parsers tolerantes (también probados directamente en tests/unit)
# --------------------------------------------------------------------------- #
def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def parse_external_task_id(raw: Any) -> str | None:
    """Extrae el id externo: directo, anidado o desde resourceURL (doble barra)."""
    if not isinstance(raw, dict):
        return None
    direct = _first_str(raw.get("id"), raw.get("sms_id"), raw.get("taskId"))
    if direct:
        return direct
    for envelope in ("data", "result"):
        nested = raw.get(envelope)
        if isinstance(nested, dict):
            direct = _first_str(nested.get("id"), nested.get("taskId"), nested.get("sms_id"))
            if direct:
                return direct
    url = _first_str(raw.get("resourceURL"), raw.get("resourceUrl"))
    if url:
        # La ruta real llega con DOBLE barra: 'send_sms//status/{id}'; el patrón
        # '/+' la tolera, y también la forma normal con una barra.
        match = re.search(r"send_sms/+status/+([^/]+)/?", url)
        if match:
            return match.group(1)
    return None


STATUS_ALIASES: dict[str, str] = {
    # delivered
    "delivered": "delivered",
    "received": "delivered",
    "acknowledged": "delivered",
    "completed": "delivered",
    "success": "delivered",
    "sent_delivered": "delivered",
    # sent
    "sent": "sent",
    "submitted": "sent",
    "accepted": "sent",
    "in_progress": "sent",
    "processing": "sent",
    "queued": "sent",
    # pending
    "pending": "pending",
    "created": "pending",
    "new": "pending",
    "scheduled": "pending",
    # failed
    "failed": "failed",
    "error": "failed",
    "rejected": "failed",
    "undelivered": "failed",
    "expired": "failed",
    "not_delivered": "failed",
    # cancelled
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "stopped": "cancelled",
}


def normalize_status(value: str | None) -> str:
    """Normaliza el estado crudo: trim + lowercase + [\\s-]→_ + tabla de la guía."""
    if not value:
        return "unknown"
    normalized = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return STATUS_ALIASES.get(normalized, "unknown")


def parse_status_payload(raw: Any) -> ProviderStatus:
    """Estado desde 'deliveryReport' con alias deliveryStatus/status/state.

    Tolerante al envoltorio ``{data: {...}}`` (guía, §2.2).
    """
    data = raw if isinstance(raw, dict) else {}
    nested = data.get("data")
    if isinstance(nested, dict) and not any(
        key in data for key in ("deliveryReport", "dateSubmitted", "content")
    ):
        data = nested
    report = data.get("deliveryReport")
    entries = report if isinstance(report, list) else []
    first = entries[0] if entries and isinstance(entries[0], dict) else None
    raw_status = _first_str(
        first.get("deliveryStatus") if first else None,
        first.get("status") if first else None,
        first.get("state") if first else None,
        # Tolerancia: estado a nivel raíz si no hay reporte.
        data.get("deliveryStatus"),
        data.get("status"),
        data.get("state"),
    )
    return ProviderStatus(
        raw_status=raw_status,
        status=normalize_status(raw_status),
        content=_first_str(data.get("content"), data.get("message"), data.get("text")),
        date_submitted=_first_str(data.get("dateSubmitted"), data.get("startDate")),
        raw=data,
    )


def parse_sms_collection(raw: Any) -> list[SmsDetail]:
    """Lista de detalles: array plano o {results}/{data}/{deliveryReport}."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        envelope = raw.get("results")
        if not isinstance(envelope, list):
            envelope = raw.get("data")
        if not isinstance(envelope, list):
            envelope = raw.get("deliveryReport")
        items = envelope if isinstance(envelope, list) else []
    else:
        items = []
    details: list[SmsDetail] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # El iccid puede venir dentro del primer elemento del deliveryReport.
        report = item.get("deliveryReport")
        entries = report if isinstance(report, list) else []
        first_entry = entries[0] if entries and isinstance(entries[0], dict) else None
        details.append(
            SmsDetail(
                iccid=_first_str(
                    item.get("iccid"),
                    first_entry.get("iccid") if first_entry else None,
                ),
                content=_first_str(item.get("content"), item.get("message"), item.get("text")),
                date_submitted=_first_str(item.get("dateSubmitted"), item.get("startDate")),
                raw_status=_first_str(
                    item.get("status"), item.get("state"), item.get("deliveryStatus")
                ),
            )
        )
    return details


# --------------------------------------------------------------------------- #
# Proveedor: protocolo común de transporte
# --------------------------------------------------------------------------- #
class Provider(Protocol):
    """Transporte de 1GLOBAL (live HTTP o replay simulado)."""

    async def submit_sms(self, *, iccid: str, text_payload: str, request_id: str) -> SendSubmitResult:
        ...

    async def get_status(self, external_task_id: str) -> ProviderStatus:
        ...

    async def list_details(
        self, *, date_from: datetime, iccid: str, page: int = 1, per_page: int = 100
    ) -> list[SmsDetail]:
        ...

    async def close(self) -> None:
        ...


# --------------------------------------------------------------------------- #
# Transporte LIVE (HTTP real)
# --------------------------------------------------------------------------- #
class LiveProvider:
    """HTTP real contra iot.truphone.com con la política de fiabilidad de la guía."""

    def __init__(self, settings) -> None:
        token = settings.oneglobal_token
        if not token:
            # El modo live exige token validado en Settings; defensa extra.
            raise ProviderError("El modo live exige un token de API (GLOBAL_API); no se inicia el transporte real.")
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.oneglobal_base_url,
            timeout=settings.http_timeout_seconds,
            headers={
                "Authorization": f"{settings.oneglobal_auth_scheme} {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._max_attempts = max(1, settings.http_max_retries)
        self._limiter = AsyncTokenLimiter(settings.oneglobal_rate_limit_per_minute)

    async def close(self) -> None:
        await self._client.aclose()

    # -- HTTP -------------------------------------------------------------- #
    def _error_body(self, response: httpx.Response) -> str:
        try:
            return json.dumps(response.json(), ensure_ascii=False)[:300]
        except (ValueError, TypeError):
            return response.text[:300]

    async def _get_with_retries(self, path: str, params: dict[str, str]) -> Any:
        """GET seguro: reintentos solo ante estados reintentables o fallo de red."""
        last_error: str = ""
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = redact(f"fallo de red/timeout: {type(exc).__name__}")
                if attempt >= self._max_attempts:
                    break
                await _sleep_backoff(attempt)
                continue
            if is_retryable_status(response.status_code):
                last_error = f"HTTP {response.status_code} reintentable"
                if attempt >= self._max_attempts:
                    break
                await _sleep_backoff(attempt)
                continue
            if 200 <= response.status_code < 300:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(
                        f"La API devolvió una respuesta no JSON: {exc}", status=response.status_code
                    ) from None
            raise ProviderError(
                f"La API respondió HTTP {response.status_code}: {self._error_body(response)}",
                status=response.status_code,
            )
        raise ProviderError(
            f"Se agotaron los reintentos ({self._max_attempts}) consultando a 1GLOBAL ({last_error})."
        )

    # -- Implementación ---------------------------------------------------- #
    async def submit_sms(self, *, iccid: str, text_payload: str, request_id: str) -> SendSubmitResult:
        del request_id  # informativo; 1GLOBAL no publica semántica de idempotencia
        if not iccid:
            raise ProviderError("El ICCID de destino está vacío; no se cursa el envío.")
        await self._limiter.acquire()
        try:
            response = await self._client.post(
                "/v2.0/sims/send_sms/", json={"iccid": [iccid], "text": text_payload}
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # UN POST con timeout pudo ser ACEPTADO: no se repite automáticamente.
            raise UncertainSubmission(
                "El POST de envío terminó sin respuesta (timeout/red): el envío PUDO llegar a "
                "1GLOBAL. No se repitió automáticamente; la tarea quedó 'uncertain' y debe "
                "conciliarse manualmente (listado de detalles de SMS)."
                f" Detalle: {redact(type(exc).__name__)}"
            ) from None
        if 200 <= response.status_code < 300:
            try:
                raw = response.json()
            except ValueError:
                raw = {"detail": response.text[:300]}
            external = parse_external_task_id(raw)
            if external is None:
                # Sumisión aceptada SIN id usable: incierta pero NO se reenvía.
                raise UncertainSubmission(
                    "La API aceptó el envío pero la respuesta no contiene un identificador externo "
                    "usable. La tarea quedó 'uncertain'; NO se reenvió automáticamente."
                )
            return SendSubmitResult(external_task_id=external, raw=raw, accepted=True)
        if response.status_code in (502, 503, 504, 425):
            raise UncertainSubmission(
                f"La API devolvió HTTP {response.status_code} en el POST de envío: el estado en "
                "1GLOBAL es dudoso. No se reintentó automáticamente; conciliar con el listado "
                f"de detalles. Detalle: {self._error_body(response)}"
            )
        raise ProviderError(
            f"La API rechazó el envío (HTTP {response.status_code}): {self._error_body(response)}",
            status=response.status_code,
        )

    async def get_status(self, external_task_id: str) -> ProviderStatus:
        if not external_task_id:
            raise ProviderError("Falta el identificador EXTERNO de 1GLOBAL para consultar el estado.")
        raw = await self._get_with_retries(
            f"/v2.0/sims/send_sms/status/{external_task_id}/", params={}
        )
        return parse_status_payload(raw)

    async def list_details(
        self, *, date_from: datetime, iccid: str, page: int = 1, per_page: int = 100
    ) -> list[SmsDetail]:
        params = {
            "dateSubmitted_from": format_api_date(date_from),
            "ordering": "-dateSubmitted",
            "page": str(page),
            "per_page": str(per_page),
        }
        if iccid:
            params["iccid"] = iccid
        raw = await self._get_with_retries("/v2.0/sims/send_sms/status/", params=params)
        return parse_sms_collection(raw)


async def _sleep_backoff(attempt: int) -> None:
    import asyncio

    await asyncio.sleep(backoff_delay(attempt))


# --------------------------------------------------------------------------- #
# Transporte REPLAY (fixtures, sin red)
# --------------------------------------------------------------------------- #
class ReplayProvider:
    """Simulación fiel con los JSON capturados en tests/fixtures.

    Nunca hace red y funciona sin token. Escenarios por prefijo de
    ``request_id`` (para pruebas y demo, documentado en README):

    - ``fail-…``: la API falla (HTTP 503, fixture sms_failure.json).
    - ``malformed-…``: respuesta de envío SIN id usable (sumisión incierta).
    - ``noresp-…``: ciclo completo SIN respuesta del dispositivo.
    - ``ambiguous-…``: dos candidatas no correlacionables.
    - resto: ciclo completo exitoso (envío → sent → delivered → respuesta).

    El estado de entrega avanza con las CONSULTAS: 1ª consulta 'SENT',
    2ª y siguientes 'DELIVERED' (para permitir pruebas deterministas).
    """

    def __init__(self, fixtures_dir: str | Path, now: Callable[[], float] = time.time) -> None:
        self._fixtures = Path(fixtures_dir)
        self._now = now
        self._tasks: dict[str, dict[str, Any]] = {}
        self._poll_counts: dict[str, int] = {}

    # -- helpers ----------------------------------------------------------- #
    def _fixture(self, name: str) -> dict[str, Any]:
        path = self._fixtures / name
        if not path.exists():
            raise ProviderError(f"Fixture de replay no encontrado: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _scenario(request_id: str) -> str:
        if request_id.startswith("fail-"):
            return "api_failure"
        if request_id.startswith("malformed-"):
            return "malformed"
        if request_id.startswith("noresp-"):
            return "no_response"
        if request_id.startswith("ambiguous-"):
            return "ambiguous"
        return "success"

    # -- Implementación ---------------------------------------------------- #
    async def submit_sms(self, *, iccid: str, text_payload: str, request_id: str) -> SendSubmitResult:
        scenario = self._scenario(request_id)
        if scenario == "api_failure":
            fixture = self._fixture("sms_failure.json")
            raise ProviderError(
                f"Simulación de fallo de la API (replay): {fixture.get('detail')}", status=503
            )
        raw = self._fixture("send_sms_success.json")
        if scenario == "malformed":
            raw = {"detail": "OK"}  # sin resourceURL ni id: extracción imposible
        external = parse_external_task_id(raw)
        if external is not None:
            self._tasks[external] = {
                "iccid": iccid,
                "text": text_payload,
                "submitted_at": self._now(),
                "scenario": scenario,
            }
        return SendSubmitResult(external_task_id=external, raw=raw, accepted=external is not None)

    async def get_status(self, external_task_id: str) -> ProviderStatus:
        task = self._tasks.get(external_task_id)
        if task is None:
            # Sondear con un id local o desconocido FALLA (como en la API real).
            raise ProviderError(
                "El identificador no corresponde a un envío conocido en modo replay: "
                "el estado solo se consulta con el external_task_id de 1GLOBAL."
            )
        count = self._poll_counts.get(external_task_id, 0) + 1
        self._poll_counts[external_task_id] = count
        delivery = "SENT" if count == 1 else "DELIVERED"
        submitted = datetime.fromtimestamp(task["submitted_at"])
        raw = {
            "id": external_task_id,
            "dateSubmitted": format_api_date(submitted),
            "content": task["text"],
            "deliveryReport": [
                {"iccid": task["iccid"], "deliveryStatus": delivery}
            ],
        }
        return parse_status_payload(raw)

    async def list_details(
        self, *, date_from: datetime, iccid: str, page: int = 1, per_page: int = 100
    ) -> list[SmsDetail]:
        entries: list[SmsDetail] = []
        template = self._fixture("sms_response_received.json")
        for task in sorted(self._tasks.values(), key=lambda item: -item["submitted_at"]):
            if task["iccid"] != iccid:
                continue
            submitted_dt = datetime.fromtimestamp(task["submitted_at"])
            # Eco saliente (MT): el propio comando, como lo registra la API.
            entries.append(
                SmsDetail(
                    iccid=iccid,
                    content=task["text"],
                    date_submitted=format_api_date(submitted_dt),
                    raw_status="SENT",
                )
            )
            scenario = task["scenario"]
            if scenario in ("success", "ambiguous"):
                # Respuestas del equipo (MO) ~2 min después de la entrega.
                responses: list[dict[str, Any]] = [dict(template)]
                if scenario == "ambiguous":
                    responses.append(
                        {**template, "id": "mo-otra", "content": "Respuesta de otro comando sin eco"}
                    )
                for index, item in enumerate(responses):
                    # Fecha relativa: +120 s por respuesta simulada.
                    response_dt = datetime.fromtimestamp(task["submitted_at"] + 120 * (index + 1))
                    entries.append(
                        SmsDetail(
                            iccid=item.get("iccid") or iccid,
                            content=item.get("content") or "",
                            date_submitted=format_api_date(response_dt),
                            raw_status=item.get("status"),
                        )
                    )
        # Filtro por fecha (dateSubmitted_from) y paginación simple.
        filtered = [
            detail
            for detail in entries
            if (parsed := parse_api_date(detail.date_submitted)) and parsed >= date_from
        ]
        filtered.sort(key=lambda d: parse_api_date(d.date_submitted) or datetime.now(), reverse=True)
        start = max(0, (page - 1) * per_page)
        return filtered[start : start + per_page]

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Cliente público
# --------------------------------------------------------------------------- #
class OneGlobalClient:
    """Fachada del proveedor para la capa de servicio."""

    def __init__(self, settings, provider: Provider | None = None) -> None:
        if provider is None:
            provider = (
                ReplayProvider(settings.fixtures_dir)
                if settings.oneglobal_mode == "replay"
                else LiveProvider(settings)
            )
        self._provider = provider

    @property
    def mode(self) -> str:
        return "replay" if isinstance(self._provider, ReplayProvider) else "live"

    async def send_sms(self, *, iccid: str, text_payload: str, request_id: str) -> SendSubmitResult:
        """UN ICCID por petición y el texto EXACTO (sin recortar ni alterar)."""
        return await self._provider.submit_sms(
            iccid=iccid, text_payload=text_payload, request_id=request_id
        )

    async def get_sms_status(self, external_task_id: str) -> ProviderStatus:
        """Estado del envío usando EXCLUSIVAMENTE el id externo de 1GLOBAL."""
        return await self._provider.get_status(external_task_id)

    async def list_sms_details(
        self, *, date_from: datetime, iccid: str, page: int = 1, per_page: int = 100
    ) -> list[SmsDetail]:
        return await self._provider.list_details(
            date_from=date_from, iccid=iccid, page=page, per_page=per_page
        )

    async def close(self) -> None:
        await self._provider.close()
