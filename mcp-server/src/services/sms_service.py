"""Servicio del ciclo de vida SMS: envío, estado de entrega y respuesta del equipo.

Este módulo aplica las reglas operativas de la guía y del documento de
desarrollo:

- ``send_sms``: destino resuelto en el servidor, política de seguridad, cola
  local persistente, simulación honesta en replay/dry-run y transmisión real
  solo tras las puertas configuradas. Un POST dudoso queda 'uncertain' (sin
  reenvío automático).
- ``get_sms_status``: consulta el estado DESDE la tarea local y refresca con
  el id EXTERNO; nunca sondea tareas pendientes locales y nunca interpreta la
  ausencia de reporte como entrega.
- ``get_sms_response``: correlación conservadora de la respuesta MO (eco del
  comando excluido, ventana estricta con relajación por entrega tardía,
  ambigüedad declarada en lugar de inventar respuesta).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from src.models import (
    INTERNAL_UNCERTAIN,
    TASK_STATUS_DESCRIPTIONS,
    ResponseResult,
    SendSmsInput,
    SendSmsResult,
    StatusResult,
)
from src.providers.oneglobal_client import (
    OneGlobalClient,
    ProviderError,
    SmsDetail,
    UncertainSubmission,
)
from src.security.approval import ApprovalError, ApprovalManager
from src.security.command_policy import (
    PolicyViolation,
    assert_target_allowed,
    assert_transmission_allowed,
    validate_command_name,
    validate_payload,
)
from src.services.sms_repository import SmsRepository
from src.utils.logging_utils import get_logger, mask_iccid
from src.utils.time_utils import iso, parse_iso, utc_now

logger = get_logger("service")

# Estados que el worker/servicio considera 'en vuelo' para el refresco.
POLLABLE_STATUSES = ("sent",)


def _normalize_echo(value: str) -> str:
    """Normalización SOLO para detección de eco del comando (guía §3.3)."""
    return " ".join(value.split()).lower()


class SmsService:
    """Orquesta las tres herramientas MCP sobre cliente, repositorio y seguridad."""

    def __init__(
        self,
        settings,
        client: OneGlobalClient,
        repo: SmsRepository,
        approval: ApprovalManager,
    ) -> None:
        self._cfg = settings
        self._client = client
        self._repo = repo
        self._approval = approval

    # ------------------------------------------------------------------ #
    # Helpers internos
    # ------------------------------------------------------------------ #
    async def _get_task(self, local_task_id: str) -> dict[str, Any] | None:
        return await self._repo.get_task(local_task_id)

    @staticmethod
    def _status_output(internal_status: str) -> str:
        if internal_status == INTERNAL_UNCERTAIN:
            return "unknown"
        return internal_status

    @staticmethod
    def _device_fingerprint(iccid: str) -> str:
        """Huella de 8 hex del SHA-256 del ICCID: el id local remite al dispositivo
        (semántica del proyecto previo, que armaba el id con el identificador del
        destino) sin exponer el ICCID completo al agente."""
        return hashlib.sha256(iccid.encode("utf-8")).hexdigest()[:8]

    def _new_local_id(self, iccid: str) -> str:
        return f"smsq_{self._device_fingerprint(iccid)}_{uuid.uuid4().hex[:8]}"

    async def _resolve_task(
        self, local_task_id: str | None, target: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resuelve la tarea por id local O por dispositivo ('test' → la más reciente)."""
        if local_task_id:
            return await self._get_task(local_task_id), None
        if target != "test":
            return None, (
                f"Destino no autorizado ({target!r}): la consulta por dispositivo solo admite "
                "el equipo de pruebas (target='test')."
            )
        if not self._cfg.test_iccid:
            return None, "El destino 'test' no está configurado en el servidor (falta ONEGLOBAL_TEST_ICCID)."
        task = await self._repo.get_latest_by_iccid(self._cfg.test_iccid)
        if task is None:
            return None, (
                "Aún no hay tareas registradas para el dispositivo de pruebas: "
                "envía un SMS con send_sms primero (target='test')."
            )
        return task, None

    # ------------------------------------------------------------------ #
    # send_sms
    # ------------------------------------------------------------------ #
    async def submit(self, req: SendSmsInput) -> SendSmsResult:
        """Valida, prepara o transmite; persiste SIEMPRE con idempotencia."""
        iccid = assert_target_allowed(req.target, self._cfg.test_iccid)

        if req.text is not None:
            validate_payload(req.text)
        else:
            validate_command_name(req.command or "")

        dry_run = req.dry_run if req.dry_run is not None else self._cfg.sms_dry_run
        transmission = not dry_run
        payload = req.payload

        if transmission and req.text is None:
            raise PolicyViolation(
                "Para transmitir (dry_run=false) se requiere 'text', el SMS preparado con su formato "
                "exacto (incluye los dos espacios iniciales de autenticación si corresponde). El "
                "servidor NO agrega espacios ni altera formatos: eso lo decide la skill. Para "
                "preparar desde el nombre del comando, usa dry_run=true con 'command'."
            )

        mode_is_live = self._cfg.oneglobal_mode == "live"
        approval_jti: str | None = None
        if transmission and mode_is_live:
            # Las puertas de transmisión REAL solo aplican en modo live; en
            # replay el envío es una sumisión simulada sin red (§6.1 paso 10).
            assert_transmission_allowed(
                mode_is_live=mode_is_live,
                sms_live_enabled=self._cfg.sms_live_enabled,
                require_confirmation=self._cfg.sms_require_confirmation,
                target_iccid=iccid,
                test_iccid=self._cfg.test_iccid,
                payload=payload,
                approval_verify=self._approval.verify if self._cfg.sms_require_confirmation else None,
            )
            if self._cfg.sms_require_confirmation:
                try:
                    approval_jti = self._approval.verify(
                        req.approval_token, iccid=iccid, payload=payload
                    )
                    if await self._repo.is_approval_used(approval_jti):
                        raise ApprovalError(
                            "El token de aprobación ya fue consumido: es de UN solo uso. Pide al "
                            "operador uno nuevo con 'python main.py mint'."
                        )
                except ApprovalError as exc:
                    raise PolicyViolation(exc.message) from None

        local_task_id = self._new_local_id(iccid)
        mode = "dry_run" if not transmission else ("live" if mode_is_live else "replay")
        task, created = await self._repo.create_task(
            {
                "local_task_id": local_task_id,
                "request_id": req.request_id,
                "iccid": iccid,
                "imei": self._cfg.teltonika_test_imei or None,
                "command": payload,
                "mode": mode,
                "status": "pending",
                "created_at": iso(),
            }
        )
        if not created and task is not None:
            # Idempotencia: misma solicitud, NO se vuelve a enviar.
            await self._repo.audit("DUPLICATE_REQUEST_SUPPRESSED", task["local_task_id"])
            return SendSmsResult(
                success=True,
                mode=task["mode"],
                local_task_id=task["local_task_id"],
                external_task_id=task.get("external_task_id"),
                target=req.target,
                masked_iccid=mask_iccid(task["iccid"]),
                status=self._status_output(task["status"]),
                message=(
                    "Esta solicitud ya se procesó antes: NO se repitió el envío "
                    "(idempotencia por request_id). Consulta su estado con get_sms_status."
                ),
                duplicate_request=True,
            )
        assert task is not None

        if not transmission:
            await self._repo.audit(
                "DRY_RUN_PREPARED", local_task_id, {"masked_iccid": mask_iccid(iccid)}
            )
            return SendSmsResult(
                success=True,
                mode=mode,
                local_task_id=local_task_id,
                external_task_id=None,
                target=req.target,
                masked_iccid=mask_iccid(iccid),
                status="simulated",
                message=(
                    "SMS preparado y validado. No se realizó ninguna transmisión (dry_run). "
                    "Cuando la confirmación del usuario sea explícita, repite con dry_run=false "
                    "y el mismo 'text' preparado."
                ),
            )

        # Regla anti-solape del laboratorio: UN comando activo por ICCID (§11).
        active = await self._repo.find_active_by_iccid(
            iccid, exclude_id=local_task_id, window_seconds=self._cfg.sms_response_window_seconds
        )
        if active:
            return await self._fail_task(
                task,
                f"Ya existe un comando activo para el dispositivo de pruebas "
                f"({active[0]['local_task_id']}, estado {active[0]['status']}): se prohíben "
                "transmisiones solapadas por ICCID. Consulta su estado y respuesta antes de enviar otro.",
                audit_action="OVERLAP_REJECTED",
            )
        # Límite de envíos por dispositivo y hora.
        hour_ago = iso(utc_now() - timedelta(hours=1))
        recent = await self._repo.count_recent_by_device(
            iccid, since_iso=hour_ago, exclude_id=local_task_id
        )
        if recent >= self._cfg.sms_device_max_per_hour:
            return await self._fail_task(
                task,
                f"Se alcanzó el límite de {self._cfg.sms_device_max_per_hour} SMS por hora para el "
                "dispositivo de pruebas (protección anti-bucles del LLM).",
                audit_action="DEVICE_RATE_REJECTED",
            )

        # Transmisión (replay o live): UN ICCID por petición, texto exacto.
        try:
            result = await self._client.send_sms(
                iccid=iccid, text_payload=payload, request_id=req.request_id or local_task_id
            )
        except UncertainSubmission as exc:
            await self._repo.update_task(
                local_task_id, {"status": INTERNAL_UNCERTAIN, "error_message": exc.message}
            )
            await self._repo.audit("UNCERTAIN_SUBMISSION", local_task_id, {"error": exc.message})
            return SendSmsResult(
                success=False,
                mode=mode,
                local_task_id=local_task_id,
                external_task_id=None,
                target=req.target,
                masked_iccid=mask_iccid(iccid),
                status=INTERNAL_UNCERTAIN,
                message=(
                    f"{exc.message} Consulta get_sms_status más tarde o solicita al operador la "
                    "conciliación manual contra el listado de detalles de SMS."
                ),
                uncertain_submission=True,
            )
        except ProviderError as exc:
            return await self._fail_task(task, exc.message, audit_action="PROVIDER_REJECTED")

        external = result.external_task_id
        updates: dict[str, Any] = {
            "submitted_at": iso(),
            "send_attempts": 1,
            "external_task_id": external,
            "status": "sent" if external else INTERNAL_UNCERTAIN,
        }
        if external is None:
            updates["error_message"] = "Sumisión aceptada sin identificador externo usable."
        await self._repo.update_task(local_task_id, updates)
        await self._repo.merge_result_details(
            local_task_id, {"accepted": True, "raw": result.raw}
        )
        await self._repo.audit(
            "PROVIDER_SUBMITTED", local_task_id, {"mode": mode, "external": bool(external)}
        )
        if approval_jti:
            await self._repo.mark_approval_used(approval_jti, local_task_id)

        return SendSmsResult(
            success=True,
            mode=mode,
            local_task_id=local_task_id,
            external_task_id=external,
            target=req.target,
            masked_iccid=mask_iccid(iccid),
            status="sent" if external else INTERNAL_UNCERTAIN,
            message=(
                (
                    "SMS transmitido en modo replay: SIMULACIÓN sin transmisión real. "
                    if mode == "replay"
                    else "SMS transmitido a 1GLOBAL. "
                )
                + "La aceptación de la API NO es confirmación de entrega: consulta get_sms_status."
            )
            if external
            else "La API aceptó el envío sin identificador externo usable; tarea 'uncertain'.",
        )

    async def _fail_task(self, task: dict[str, Any], message: str, *, audit_action: str) -> SendSmsResult:
        await self._repo.update_task(task["local_task_id"], {"status": "failed", "error_message": message})
        await self._repo.audit(audit_action, task["local_task_id"], {"error": message})
        return SendSmsResult(
            success=False,
            mode=task["mode"],
            local_task_id=task["local_task_id"],
            external_task_id=task.get("external_task_id"),
            target="test",
            masked_iccid=mask_iccid(task["iccid"]),
            status="failed",
            message=message,
        )

    # ------------------------------------------------------------------ #
    # get_sms_status
    # ------------------------------------------------------------------ #
    async def status(
        self, local_task_id: str | None = None, *, target: str = "test"
    ) -> StatusResult:
        task, missing = await self._resolve_task(local_task_id, target)
        if task is None:
            return StatusResult(
                success=False,
                local_task_id=local_task_id or "",
                external_task_id=None,
                status="unknown",
                status_description=TASK_STATUS_DESCRIPTIONS["unknown"],
                command="",
                masked_iccid="",
                mode="",
                message=missing
                or "No existe una tarea local con ese identificador; usa el local_task_id "
                "devuelto por send_sms o consulta por dispositivo (target='test').",
            )
        local_task_id = task["local_task_id"]

        refresh_attempted = False
        refresh_error: str | None = None
        external = task.get("external_task_id")

        # SOLO tareas 'sent' se refrescan contra 1GLOBAL (con id EXTERNO);
        # las 'pending' locales no existen allí y no se sondean (guía §2.3).
        if task["status"] in POLLABLE_STATUSES and external and task["mode"] in ("replay", "live"):
            refresh_attempted = True
            try:
                provider_status = await self._client.get_sms_status(external)
                delivered = provider_status.status == "delivered"
                updates: dict[str, Any] = {
                    "status": provider_status.status,
                    "last_checked_at": iso(),
                    "poll_attempts": task.get("poll_attempts", 0) + 1,
                }
                if delivered and not task.get("delivered_at"):
                    updates["delivered_at"] = iso()
                await self._repo.update_task(local_task_id, updates)
                await self._repo.merge_result_details(
                    local_task_id,
                    {"raw_status": provider_status.raw_status, "raw": provider_status.raw},
                )
                if provider_status.status != task["status"]:
                    await self._repo.audit(
                        "SMS_STATUS_CHANGE",
                        local_task_id,
                        {"from": task["status"], "to": provider_status.status},
                    )
                task = (await self._get_task(local_task_id)) or task
            except ProviderError as exc:
                refresh_error = exc.message
                await self._repo.update_task(
                    local_task_id, {"last_checked_at": iso(), "error_message": exc.message}
                )

        internal = task["status"]
        delivered_at = task.get("delivered_at")
        if internal == INTERNAL_UNCERTAIN:
            message = (
                "Sumisión incierta: el POST pudo o no haber llegado a 1GLOBAL y no hay id externo "
                "para sondear. Conciliación manual sugerida: listar detalles de SMS del ICCID "
                "(operador) y comparar el contenido enviado. NO se reenvió automáticamente."
            )
        elif internal == "pending":
            message = (
                "Tarea preparada sin transmisión (dry_run) o pendiente: no se sondea a 1GLOBAL "
                "porque aún no tiene id externo."
            )
        elif internal == "delivered":
            message = "Confirmación de entrega recibida de 1GLOBAL."
        elif refresh_error:
            message = f"Se muestra el último estado conocido; el refresco externo falló: {refresh_error}"
        else:
            message = TASK_STATUS_DESCRIPTIONS.get(internal, "Estado sin descripción.")

        return StatusResult(
            success=True,
            local_task_id=local_task_id,
            external_task_id=external,
            status=self._status_output(internal),
            status_description=TASK_STATUS_DESCRIPTIONS.get(
                self._status_output(internal), "Sin descripción."
            ),
            command=task["command"],
            masked_iccid=mask_iccid(task["iccid"]) or "",
            mode=task["mode"],
            delivered_at=delivered_at,
            last_checked_at=task.get("last_checked_at"),
            refresh_attempted=refresh_attempted,
            refresh_error=refresh_error,
            message=message,
        )

    # ------------------------------------------------------------------ #
    # get_sms_response
    # ------------------------------------------------------------------ #
    async def response(
        self,
        local_task_id: str | None = None,
        *,
        target: str = "test",
        wait_seconds: int = 0,
    ) -> ResponseResult:
        # La espera activa está acotada por configuración (defensa del tiempo
        # de la llamada MCP). El agente puede esperar hasta ese máximo en la
        # MISMA llamada mientras el worker externo detecta en paralelo.
        wait_seconds = max(0, min(int(wait_seconds), self._cfg.sms_response_wait_max_seconds))
        task, missing = await self._resolve_task(local_task_id, target)
        if task is None:
            return ResponseResult(
                success=False,
                local_task_id=local_task_id or "",
                found=False,
                command="",
                message=missing
                or "No existe una tarea local con ese identificador; consulta por "
                "dispositivo (target='test') o usa el local_task_id de send_sms.",
            )
        local_task_id = task["local_task_id"]
        base = dict(
            success=True,
            local_task_id=local_task_id,
            command=task["command"],
        )
        if task.get("response_received"):
            return ResponseResult(
                **base,
                found=True,
                response_content=task.get("response_content"),
                detected_at=task.get("response_at"),
                message="Respuesta del dispositivo detectada y persistida.",
            )
        if task["mode"] == "dry_run" or task["status"] == "pending":
            return ResponseResult(
                **base,
                found=False,
                message="No hubo transmisión real: no puede existir respuesta del dispositivo.",
            )
        if task["status"] in ("failed", "cancelled"):
            return ResponseResult(
                **base,
                found=False,
                message=f"Tarea en estado '{task['status']}': no habrá respuesta del dispositivo.",
            )
        if task["status"] != "delivered":
            return ResponseResult(
                **base,
                found=False,
                message=(
                    f"El SMS aún no se ha entregado (estado '{task['status']}'); la respuesta del "
                    "equipo llega después de la entrega. Consulta get_sms_status primero."
                ),
            )

        # Espera activa acotada: sondeo interno (≤3 s por intento) mientras el
        # worker externo también detecta en paralelo; aborta al detectar.
        if wait_seconds > 0:
            import asyncio as _asyncio

            deadline = utc_now() + timedelta(seconds=wait_seconds)
            while not task.get("response_received"):
                outcome = await self._detect_response(task)
                if not isinstance(outcome, str):
                    break  # respuesta detectada y persistida
                fresh = await self._get_task(local_task_id) or task
                if fresh.get("response_received"):
                    task = fresh
                    break
                restante = (deadline - utc_now()).total_seconds()
                if restante <= 0:
                    logger.info(
                        "Espera agotada (%s s) sin respuesta aún para %s: el worker sigue sondeando.",
                        wait_seconds, local_task_id,
                    )
                    return ResponseResult(
                        **base,
                        found=False,
                        ambiguous=(outcome == "ambiguous"),
                        message=(
                            f"Sin respuesta del dispositivo en la espera de {wait_seconds} s. "
                            "El worker sigue sondeando en el servidor y la respuesta queda "
                            "persistida cuando llegue: vuelve a llamar get_sms_response más tarde "
                            "(o agenda un recordatorio) y se la devuelvo desde la BD."
                        ),
                    )
                await _asyncio.sleep(min(3.0, restante))
                local_task_ref = await self._get_task(local_task_id)
                if local_task_ref is not None:
                    task = local_task_ref
                continue
            if task.get("response_received"):
                fresh = (await self._get_task(local_task_id)) or task
                return ResponseResult(
                    **base,
                    found=True,
                    response_content=fresh.get("response_content"),
                    detected_at=fresh.get("response_at"),
                    message="Respuesta del dispositivo detectada y persistida.",
                )

        outcome = await self._detect_response(task)
        if isinstance(outcome, str):
            return ResponseResult(
                **base,
                found=False,
                ambiguous=(outcome == "ambiguous"),
                message=(
                    "Se encontraron respuestas candidatas no correlacionables: se reporta "
                    "'ambiguous' en lugar de presentar una respuesta como definitiva."
                    if outcome == "ambiguous"
                    else "Sin respuesta del dispositivo dentro de la ventana configurada. "
                    "Puede llegar hasta minutos después de la entrega: vuelve a consultar."
                ),
            )
        fresh = (await self._get_task(local_task_id)) or task
        return ResponseResult(
            **base,
            found=True,
            response_content=fresh.get("response_content"),
            detected_at=fresh.get("response_at"),
            message="Respuesta del dispositivo detectada y persistida.",
        )

    async def _detect_response(self, task: dict[str, Any]) -> str | dict[str, Any]:
        """Correlación conservadora (guía §3.3). Devuelve 'none' | 'ambiguous' | dict."""
        created = parse_iso(task.get("created_at"))
        if created is None:
            return "none"
        now = utc_now()
        if now - created > timedelta(seconds=self._cfg.sms_response_window_seconds):
            return "none"
        lookback = timedelta(seconds=self._cfg.response_lookback_seconds)
        since = created - lookback

        # Regla anti-atribución cruzada: si hay OTRA tarea entregada sin respuesta
        # para el mismo ICCID, la correlación no es segura.
        others = await self._repo.find_active_by_iccid(
            task["iccid"], exclude_id=task["local_task_id"], window_seconds=self._cfg.sms_response_window_seconds
        )
        if any(other["status"] == "delivered" for other in others):
            return "ambiguous"

        details = await self._fetch_details(since, task["iccid"])
        comando_norm = _normalize_echo(task["command"])
        candidates = [
            detail
            for detail in details
            if detail.content and _normalize_echo(detail.content) != comando_norm
        ]
        if not candidates:
            return "none"

        delivered = parse_iso(task.get("delivered_at"))
        floor = (delivered - lookback) if delivered else since
        late_delivery = delivered is not None and (delivered - created) > lookback
        if late_delivery:
            floor = since

        def within_window(detail: SmsDetail) -> bool:
            date = _detail_date(detail)
            # Fechas ausentes se conservan (criterio de la guía §3.3).
            return date is None or date >= floor

        strict = [detail for detail in candidates if within_window(detail)]
        chosen_list = strict or candidates
        unique_contents = {_normalize_echo(c.content or "") for c in chosen_list}
        if len(unique_contents) > 1:
            return "ambiguous"
        chosen = chosen_list[0]
        response_at = iso(now)
        await self._repo.update_task(
            task["local_task_id"],
            {
                "response_received": True,
                "response_content": chosen.content,
                "response_at": response_at,
            },
        )
        await self._repo.audit(
            "COMMAND_RESPONSE_RECEIVED",
            task["local_task_id"],
            {"iccid_masked": mask_iccid(task["iccid"])},
        )
        return {"content": chosen.content, "at": response_at}

    async def _fetch_details(self, since: datetime, iccid: str) -> list[SmsDetail]:
        collected: list[SmsDetail] = []
        page = 1
        while page <= 3:  # se buscan páginas siguientes solo si hace falta
            batch = await self._client.list_sms_details(date_from=since, iccid=iccid, page=page)
            collected.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return collected

    # ------------------------------------------------------------------ #
    # Ciclo de vida para el worker
    # ------------------------------------------------------------------ #
    async def poll_sent_task(self, task: dict[str, Any]) -> None:
        """Sondea UNA tarea 'sent' con id externo y aplica la gracia de entrega."""
        local_task_id = task["local_task_id"]
        external = task.get("external_task_id")
        if not external:
            return
        attempts = task.get("poll_attempts", 0) + 1
        try:
            provider_status = await self._client.get_sms_status(external)
        except ProviderError as exc:
            exhausted = attempts >= self._cfg.sms_max_retries
            created = parse_iso(task.get("created_at"))
            within_grace = (
                created is not None
                and (utc_now() - created) < timedelta(seconds=self._cfg.sms_sent_grace_seconds)
            )
            # La gracia protege TAMBIÉN ante errores de la API: una caída breve
            # de 1GLOBAL NO cierra la tarea (guía §2.3).
            if exhausted and not within_grace:
                await self._repo.update_task(
                    local_task_id,
                    {
                        "status": "failed",
                        "poll_attempts": attempts,
                        "last_checked_at": iso(),
                        "error_message": exc.message
                        + " (reintentos agotados y fuera del período de gracia)",
                    },
                )
                await self._repo.audit("SMS_STATUS_CHANGE", local_task_id, {"to": "failed"})
            else:
                await self._repo.update_task(
                    local_task_id,
                    {"last_checked_at": iso(), "poll_attempts": attempts},
                )
                logger.warning(
                    "Reintento %s/%s para el SMS %s: %s",
                    attempts,
                    self._cfg.sms_max_retries,
                    local_task_id,
                    exc.message,
                )
            return

        previous = task["status"]
        updates: dict[str, Any] = {
            "status": provider_status.status,
            "last_checked_at": iso(),
            "poll_attempts": attempts,
        }
        if provider_status.status == "delivered" and not task.get("delivered_at"):
            updates["delivered_at"] = iso()
        await self._repo.update_task(local_task_id, updates)
        await self._repo.merge_result_details(
            local_task_id, {"raw_status": provider_status.raw_status, "raw": provider_status.raw}
        )
        if provider_status.status != previous:
            await self._repo.audit(
                "SMS_STATUS_CHANGE", local_task_id, {"from": previous, "to": provider_status.status}
            )

    async def detect_response_for_delivered(self, task: dict[str, Any]) -> None:
        """Busca la respuesta del equipo para una tarea entregada (sin lanzar)."""
        try:
            await self._detect_response(task)
        except (ProviderError, ValueError) as exc:
            # Un fallo aquí no rompe la pasada del worker (guía §2.3).
            logger.warning("Detección de respuesta falló para %s: %s", task["local_task_id"], exc)


def _detail_date(detail: SmsDetail) -> datetime | None:
    from src.utils.time_utils import parse_api_date

    return parse_api_date(detail.date_submitted)
