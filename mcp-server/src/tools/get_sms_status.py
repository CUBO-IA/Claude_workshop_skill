"""Herramienta MCP ``get_sms_status``.

Consulta el estado de entrega de un SMS previamente enviado, usando su
identificador de tarea local (o ``reference_id``, alias de la skill).
"""

from __future__ import annotations

from pydantic import ValidationError

from src.models import GetStatusInput, StatusResult
from src.services.sms_service import SmsService

TOOL_DESCRIPTION = (
    "Consulta el estado de entrega de un SMS previamente enviado mediante 1GLOBAL, utilizando su "
    "identificador de tarea local (local_task_id o reference_id); si no lo indicas, devuelve la "
    "tarea MÁS RECIENTE del dispositivo de pruebas (target='test', resuelto en el servidor). "
    "Distingue aceptación por la API, entrega confirmada y estados inciertos; nunca interpreta la "
    "ausencia de reporte como entrega."
)


def register(mcp, service: SmsService) -> None:
    @mcp.tool(name="get_sms_status", description=TOOL_DESCRIPTION)
    async def get_sms_status(
        local_task_id: str | None = None,
        reference_id: str | None = None,
        target: str = "test",
    ) -> StatusResult:
        requested_id = local_task_id or reference_id or None
        try:
            req = GetStatusInput(local_task_id=local_task_id, reference_id=reference_id, target=target)
        except ValidationError:
            return StatusResult(
                success=False,
                local_task_id=requested_id or "",
                external_task_id=None,
                status="unknown",
                status_description="Entrada inválida.",
                command="",
                masked_iccid="",
                mode="",
                message=(
                    "Indica la tarea (local_task_id o reference_id) o usa target='test' para "
                    "resolver la última tarea del dispositivo de pruebas."
                ),
            )
        return await service.status(requested_id, target=req.target)
