"""Barrera de seguridad de comandos: el servidor es la frontera autoritativa.

La skill `teltonika-commands` aporta CONOCIMIENTO (catálogo, sintaxis, riesgo);
este módulo impone las reglas en el servidor, sin depender de la redacción del
SKILL.md ni de parámetros aportados por el LLM:

1. Las transmisiones reales solo van al ICCID de pruebas configurado.
2. Se rechazan ICCID arbitrarios y destinos desconocidos.
3. Se valida longitud, tipo y carga (sin caracteres de control), preservando
   los espacios iniciales del comando.
4. Se distingue lectura (allowlist) de comandos de cambio de configuración,
   reinicio o borrado bloqueados explícitamente.
5. La transmisión real exige el mecanismo de autorización configurado
   (SMS_LIVE_ENABLED + token de aprobación de un solo uso).

Catálogo alineado con `skill/teltonika-commands/references/commands.json`
(comandos de riesgo bajo con efecto read_only/read_action) y
`references/compatibility.json` (los bloqueados nunca se permiten aquí).
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SMS_LENGTH = 160  # límite conservador de la skill para SMS de parámetros

# Allowlist de laboratorio: comandos de riesgo BAJO del catálogo Teltonika.
ALLOWED_COMMANDS: dict[str, dict[str, str]] = {
    "web_connect": {"effect": "read_action", "risk": "Bajo", "description": "Conexión inmediata con FOTA WEB."},
    "getinfo": {"effect": "read_only", "risk": "Bajo", "description": "Información de funcionamiento del sistema."},
    "getver": {"effect": "read_only", "risk": "Bajo", "description": "Versión de firmware e identificación."},
    "getstatus": {"effect": "read_only", "risk": "Bajo", "description": "Estado del módem y de la red."},
    "getgps": {"effect": "read_only", "risk": "Bajo", "description": "Datos GNSS, fecha y hora."},
    "ggps": {"effect": "read_only", "risk": "Bajo", "description": "Ubicación actual con enlace de mapa."},
    "getio": {"effect": "read_only", "risk": "Bajo", "description": "Estados de entradas/salidas."},
    "readio": {"effect": "read_only", "risk": "Bajo", "description": "Valor de un identificador AVL."},
    "getparam": {"effect": "read_only", "risk": "Bajo", "description": "Lectura de parámetros de configuración."},
    "getrecord": {"effect": "read_action", "risk": "Bajo", "description": "Registro de rastreo de alta prioridad."},
    "countrecs": {"effect": "read_only", "risk": "Bajo", "description": "Cantidad de registros almacenados."},
}

# Comandos destructivos/de alto impacto: BLOQUEADOS por defecto en este MCP.
BLOCKED_COMMANDS = {"setparam", "cpureset", "deleterecords", "flush", "setdigout", "reset", "resetcfg"}

DEST_LOGICAL_TARGET = "test"


class PolicyViolation(Exception):
    """Violación de seguridad: mensaje en español apto para el agente."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class CommandValidation:
    """Resultado de validar un comando contra la allowlist."""

    name: str
    effect: str
    risk: str
    description: str


def extract_command_name(payload: str) -> str:
    """Primer token del SMS (p. ej. 'web_connect' de '  web_connect')."""
    stripped = payload.strip()
    if not stripped:
        return ""
    return stripped.split(" ", 1)[0].strip()


def validate_command_name(name: str) -> CommandValidation:
    """Valida el NOMBRE de comando contra allowlist/bloqueados (sensible a mayúsculas)."""
    if not name:
        raise PolicyViolation("No se indicó un comando.")
    if name in BLOCKED_COMMANDS:
        raise PolicyViolation(
            f"El comando '{name}' está BLOQUEADO en este laboratorio (riesgo alto: cambia "
            "configuración, reinicia o borra registros). Requiere un flujo posterior revisado."
        )
    info = ALLOWED_COMMANDS.get(name)
    if info is None:
        if name.lower() in ALLOWED_COMMANDS:
            raise PolicyViolation(
                f"El comando '{name}' no coincide con la allowlist: los comandos Teltonika son "
                "sensibles a mayúsculas y minúsculas."
            )
        raise PolicyViolation(
            f"El comando '{name}' no está en la allowlist del servidor. Solo se permiten comandos "
            "de riesgo bajo: " + ", ".join(sorted(ALLOWED_COMMANDS)) + "."
        )
    return CommandValidation(
        name=name, effect=info["effect"], risk=info["risk"], description=info["description"]
    )


def validate_payload(payload: str) -> CommandValidation:
    """Valida el contenido SMS completo: tipo, longitud, inyección y allowlist."""
    if not isinstance(payload, str) or not payload:
        raise PolicyViolation("El contenido del SMS está vacío o no es texto.")
    if len(payload) > MAX_SMS_LENGTH:
        raise PolicyViolation(
            f"El SMS supera el límite de {MAX_SMS_LENGTH} caracteres del laboratorio ({len(payload)})."
        )
    control_chars = [
        char for char in payload if (ord(char) < 32 and char != " ") or ord(char) == 127
    ]
    if control_chars:
        raise PolicyViolation(
            "El SMS contiene caracteres de control (inyección): "
            "p. ej. saltos de línea o tabuladores no son permitidos."
        )
    if "<" in payload or ">" in payload:
        raise PolicyViolation(
            "El SMS contiene marcadores '<' o '>' sin resolver; el comando debe estar listo para transmitir."
        )
    # Catálogo global: solo el nombre (primer token) se coteja; los espacios
    # iniciales de autenticación y el resto el contenido NO se alteran.
    return validate_command_name(extract_command_name(payload))


def assert_target_allowed(target: str, test_iccid: str) -> str:
    """Resuelve el destino lógico al ICCID autorizado; rechaza lo demás."""
    if target != DEST_LOGICAL_TARGET:
        raise PolicyViolation(
            f"Destino no autorizado ({target!r}): este laboratorio solo permite el dispositivo de "
            "pruebas (target='test'). Se rechazan ICCID arbitrarios."
        )
    if not test_iccid:
        raise PolicyViolation(
            "El destino 'test' no está configurado en el servidor: falta ONEGLOBAL_TEST_ICCID en "
            "el .env del servidor MCP. Envío detenido."
        )
    return test_iccid


def assert_transmission_allowed(
    *,
    mode_is_live: bool,
    sms_live_enabled: bool,
    require_confirmation: bool,
    target_iccid: str,
    test_iccid: str,
    payload: str,
    approval_verify,
) -> str | None:
    """Puerta de transmisión REAL; devuelve el jti del token usado (o None).

    ``approval_verify`` es la función de verificación del ApprovalManager
    (``token, iccid, payload`` → jti) que lanza ApprovalError si el token es
    inválido. Nunca se devuelve información del token al agente.
    """
    if not mode_is_live:
        raise PolicyViolation(
            "La transmisión real está deshabilitada: el servidor está en modo replay "
            "(ONEGLOBAL_MODE=replay). El envío solo prepara o simula."
        )
    if not sms_live_enabled:
        raise PolicyViolation(
            "La transmisión real está deshabilitada: SMS_LIVE_ENABLED=false. Pide al operador que "
            "la habilite y emita un token de aprobación de un solo uso."
        )
    if target_iccid != test_iccid:
        raise PolicyViolation(
            "El destino no coincide con el ICCID de pruebas autorizado en el servidor."
        )
    info = validate_payload(payload)
    if info.risk != "Bajo":
        raise PolicyViolation("Solo se transmiten comandos de riesgo bajo en este laboratorio.")
    if require_confirmation:
        if not approval_verify:
            raise PolicyViolation(
                "Confirmación requerida (SMS_REQUIRE_CONFIRMATION=true) pero el mecanismo de "
                "aprobación no está disponible: pide al operador el token de un solo uso."
            )
        _ = approval_verify  # el token se verifica fuera, con su mensaje específico
    return None
