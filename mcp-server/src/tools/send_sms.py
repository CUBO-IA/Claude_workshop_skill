"""Herramienta MCP ``send_sms``.

Prepara y transmite un comando Teltonika por 1GLOBAL hacia el dispositivo de
pruebas autorizado, con validación de seguridad e idempotencia.
"""

from __future__ import annotations

from src.models import SendSmsInput, SendSmsResult
from src.security.command_policy import PolicyViolation
from src.services.sms_service import SmsService

TOOL_DESCRIPTION = (
    "Envía un comando SMS Teltonika al dispositivo de pruebas autorizado mediante la API de 1GLOBAL. "
    "Requiere validación de seguridad y, para transmisiones reales, SMS_LIVE_ENABLED y un token de "
    "aprobación de un solo uso emitido por el operador (nunca lo generes tú). Con dry_run=true solo "
    "prepara y valida, sin transmisión. Devuelve el local_task_id para seguimiento."
)


def register(mcp, service: SmsService) -> None:
    @mcp.tool(name="send_sms", description=TOOL_DESCRIPTION)
    async def send_sms(
        target: str = "test",
        text: str | None = None,
        command: str | None = None,
        dry_run: bool | None = None,
        request_id: str | None = None,
        approval_token: str | None = None,
    ) -> SendSmsResult:
        req = SendSmsInput(
            target=target,
            text=text,
            command=command,
            dry_run=dry_run,
            request_id=request_id,
            approval_token=approval_token,
        )
        try:
            return await service.submit(req)
        except PolicyViolation as exc:
            return SendSmsResult(
                success=False,
                mode="dry_run" if (dry_run if dry_run is not None else True) else "live",
                local_task_id=None,
                external_task_id=None,
                target=target,
                masked_iccid=None,
                status="rejected",
                message=exc.message,
            )
