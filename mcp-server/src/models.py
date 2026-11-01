"""Modelos Pydantic de las herramientas MCP y del ciclo de vida de tareas.

Los campos y mensajes están en español: son las descripciones que el agente
verá en el esquema publicado de las herramientas MCP.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class SmsStatus(StrEnum):
    """Estados normalizados de una tarea SMS (admitidos por el MCP)."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# Estado interno adicional: sumisión incierta (POST dudoso sin confirmación).
# No se expone como estado normalizado; get_sms_status lo informa como
# 'unknown' con explicación y vía de conciliación.
INTERNAL_UNCERTAIN = "uncertain"

# Estado interno para tareas dry-run: preparadas, jamás transmitidas.
INTERNAL_SIMULATED = "simulated"

TASK_STATUS_DESCRIPTIONS: dict[str, str] = {
    "pending": "Encolada localmente; aún sin confirmación de 1GLOBAL.",
    "sent": "Transmitida a 1GLOBAL; en camino hacia el dispositivo.",
    "delivered": "Entregada al dispositivo (confirmado por 1GLOBAL).",
    "failed": "La transmisión o la entrega fallaron.",
    "cancelled": "Cancelada; no continuará.",
    "unknown": "Sin confirmación de entrega todavía (puede ser una sumisión incierta).",
    "uncertain": "Sumisión incierta: el POST pudo o no haber llegado a 1GLOBAL; conciliar manualmente.",
    "simulated": "Preparada y validada sin transmisión (dry_run).",
}


class TaskMode(StrEnum):
    """Modo con el que se creó la tarea."""

    DRY_RUN = "dry_run"
    REPLAY = "replay"
    LIVE = "live"


# --------------------------------------------------------------------------- #
# Entradas de las herramientas MCP
# --------------------------------------------------------------------------- #
class SendSmsInput(BaseModel):
    """Entrada de ``send_sms``.

    Compatibilidad con la skill `teltonika-commands`:
    - ``text``: SMS ya preparado por la skill (incluye los dos espacios
      iniciales de autenticación si corresponde). Se conserva tal cual.
    - ``command``: alias del contrato propuesto; solo sirve para preparar
      (``dry_run``). El servidor NUNCA antepone espacios de autenticación
      automáticamente: el formato exacto lo determina la skill.
    """

    target: str = Field(
        "test",
        description=(
            "Destino lógico del SMS. En este laboratorio solo está autorizado 'test' "
            "(el dispositivo de pruebas configurado en el servidor). Se rechazan ICCID arbitrarios."
        ),
    )
    text: str | None = Field(
        None,
        description=(
            "Contenido SMS exacto ya formado por la skill (por ejemplo '  web_connect', "
            "con sus espacios iniciales). No se recorta ni se altera. Obligatorio para transmitir."
        ),
    )
    command: str | None = Field(
        None,
        description="Nombre del comando Teltonika (alias de compatibilidad). Solo con dry_run.",
    )
    dry_run: bool | None = Field(
        None,
        description=(
            "true: preparar y validar sin transmitir. Si se omite, se aplica SMS_DRY_RUN del servidor "
            "(true por defecto: nunca transmite sin configuración explícita)."
        ),
    )
    request_id: str | None = Field(
        None,
        description=(
            "Identificador único de la solicitud para idempotencia. Si se repite, el servidor "
            "NO vuelve a enviar el SMS y devuelve la tarea original."
        ),
    )
    approval_token: str | None = Field(
        None,
        description=(
            "Token de aprobación de un solo uso, emitido por el operador fuera del modelo "
            "(CLI del servidor). Obligatorio para transmisiones reales cuando SMS_REQUIRE_CONFIRMATION=true."
        ),
    )

    @model_validator(mode="after")
    def _one_content_field(self) -> SendSmsInput:
        if self.text is not None and self.command is not None:
            raise ValueError(
                "Indica un solo contenido: 'text' (SMS completo preparado) o 'command' (nombre del comando)."
            )
        if self.text is None and self.command is None:
            raise ValueError("Indica el contenido del SMS: 'text' o 'command'.")
        return self

    @property
    def payload(self) -> str:
        """Contenido SMS tal como lo aporta el cliente (sin alterar)."""
        if self.text is not None:
            return self.text
        return self.command or ""


class GetStatusInput(BaseModel):
    """Entrada de ``get_sms_status`` (acepta ``reference_id`` de la skill)."""

    local_task_id: str | None = Field(None, description="Identificador de tarea local (smsq_…).")
    reference_id: str | None = Field(
        None, description="Alias de compatibilidad con la skill teltonika-commands."
    )
    target: str = Field(
        "test",
        description="Resolución por dispositivo: 'test' devuelve la última tarea del equipo de pruebas.",
    )

    @model_validator(mode="after")
    def _need_reference(self) -> GetStatusInput:
        if not self.local_task_id and not self.reference_id and self.target != "test":
            raise ValueError(
                "Indica la tarea (local_task_id o reference_id) o target='test' para "
                "resolver la última tarea del dispositivo de pruebas."
            )
        return self

    @property
    def task_id(self) -> str:
        return self.local_task_id or self.reference_id or ""


class GetResponseInput(BaseModel):
    """Entrada de ``get_sms_response`` (misma resolución que get_sms_status)."""

    local_task_id: str | None = Field(None, description="Identificador de tarea local (smsq_…).")
    reference_id: str | None = Field(
        None, description="Alias de compatibilidad con la skill teltonika-commands."
    )
    target: str = Field(
        "test",
        description="Resolución por dispositivo: 'test' devuelve la última tarea del equipo de pruebas.",
    )

    @model_validator(mode="after")
    def _need_reference(self) -> GetResponseInput:
        if not self.local_task_id and not self.reference_id and self.target != "test":
            raise ValueError(
                "Indica la tarea (local_task_id o reference_id) o target='test' para "
                "resolver la última tarea del dispositivo de pruebas."
            )
        return self

    @property
    def task_id(self) -> str:
        return self.local_task_id or self.reference_id or ""


# --------------------------------------------------------------------------- #
# Salidas de las herramientas MCP
# --------------------------------------------------------------------------- #
class SendSmsResult(BaseModel):
    """Salida de ``send_sms``."""

    success: bool = Field(description="true si la solicitud se procesó (incluye simulaciones).")
    mode: str = Field(description="Modo de la tarea: dry_run | replay | live.")
    local_task_id: str | None = Field(
        None, description="Identificador de tarea local estable; úsalo en get_sms_status/get_sms_response."
    )
    external_task_id: str | None = Field(
        None, description="Identificador de tarea en 1GLOBAL (solo si la API devolvió uno usable)."
    )
    target: str = Field(description="Destino lógico solicitado.")
    masked_iccid: str | None = Field(None, description="ICCID de destino enmascarado (nunca completo).")
    status: str = Field(
        description="simulated (dry-run) | pending | sent | uncertain | rejected | failed."
    )
    message: str = Field(description="Explicación en español del resultado.")
    uncertain_submission: bool = Field(
        False, description="true si el POST terminó dudoso: NO se reintentó automáticamente."
    )
    duplicate_request: bool = Field(
        False, description="true si request_id ya se procesó antes: no se reenvió nada."
    )


class StatusResult(BaseModel):
    """Salida de ``get_sms_status``."""

    success: bool = Field(description="true si la tarea local existe y se pudo consultar.")
    local_task_id: str = Field(description="Identificador de tarea local.")
    external_task_id: str | None = Field(None, description="Identificador externo en 1GLOBAL, si hay.")
    status: str = Field(description="Estado normalizado: pending|sent|delivered|failed|cancelled|unknown.")
    status_description: str = Field(description="Descripción del estado en español.")
    command: str = Field(description="Contenido SMS asociado a la tarea.")
    masked_iccid: str = Field(description="ICCID de destino enmascarado.")
    mode: str = Field(description="Modo de la tarea: dry_run | replay | live.")
    delivered_at: str | None = Field(None, description="Fecha UTC de entrega confirmada, si la hay.")
    last_checked_at: str | None = Field(None, description="Fecha UTC del último chequeo de estado.")
    refresh_attempted: bool = Field(
        False, description="true si se intentó refrescar el estado contra 1GLOBAL (id externo)."
    )
    refresh_error: str | None = Field(None, description="Error del refresco externo, si ocurrió.")
    message: str = Field(description="Explicación en español del resultado.")


class ResponseResult(BaseModel):
    """Salida de ``get_sms_response``."""

    success: bool = Field(description="true si la tarea local existe y se pudo consultar.")
    local_task_id: str = Field(description="Identificador de tarea local.")
    found: bool = Field(description="true solo si hay una respuesta del dispositivo confirmada.")
    ambiguous: bool = Field(
        False, description="true si hubo candidatas no correlacionables; NO se inventa respuesta."
    )
    response_content: str | None = Field(None, description="Contenido de la respuesta del equipo (MO).")
    detected_at: str | None = Field(None, description="Fecha UTC de detección de la respuesta.")
    command: str = Field(description="Comando original enviado.")
    message: str = Field(description="Explicación en español del resultado.")
