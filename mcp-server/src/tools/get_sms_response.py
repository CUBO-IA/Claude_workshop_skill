"""Herramienta MCP ``get_sms_response``.

Recupera la respuesta SMS del dispositivo Teltonika (MO) tras un comando,
correlacionándola de forma conservadora con la tarea local.
"""

from __future__ import annotations

from pydantic import ValidationError

from src.models import GetResponseInput, ResponseResult
from src.services.sms_service import SmsService

TOOL_DESCRIPTION = (
    "Consulta la respuesta SMS enviada por el dispositivo Teltonika después de recibir un comando y "
    "devuelve su contenido cuando puede correlacionarse con la tarea local; si no indicas "
    "local_task_id/reference_id, usa la tarea MÁS RECIENTE del dispositivo de pruebas "
    "(target='test', resuelto en el servidor). Excluye el eco del comando enviado; si la "
    "correlación es ambigua lo declara y NO inventa una respuesta. Opcionalmente espera "
"ACTIVAMENTE hasta 120 s (wait_seconds) por la respuesta dentro de la misma llamada: el "
"worker del servidor también sondea por su cuenta (15 s por defecto) y persiste la "
"respuesta, así que una llamada posterior la devuelve sin esperar nada."
)


def register(mcp, service: SmsService) -> None:
    @mcp.tool(name="get_sms_response", description=TOOL_DESCRIPTION)
    async def get_sms_response(
        local_task_id: str | None = None,
        reference_id: str | None = None,
        target: str = "test",
        wait_seconds: int = 0,
    ) -> ResponseResult:
        requested_id = local_task_id or reference_id or None
        try:
            req = GetResponseInput(
                local_task_id=local_task_id,
                reference_id=reference_id,
                target=target,
                wait_seconds=max(0, int(wait_seconds or 0)),
            )
        except ValidationError:
            return ResponseResult(
                success=False,
                local_task_id=requested_id or "",
                found=False,
                command="",
                message=(
                    "Indica la tarea (local_task_id o reference_id) o usa target='test' para "
                    "resolver la última tarea del dispositivo de pruebas; wait_seconds debe ser "
                    "0-120."
                ),
            )
        return await service.response(
            requested_id, target=req.target, wait_seconds=req.wait_seconds
        )
