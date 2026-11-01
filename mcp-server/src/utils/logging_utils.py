"""Logging estructurado con redacción de secretos.

Toda la salida va a **stderr**: en transporte stdio, stdout pertenece al
protocolo MCP y no debe recibir logs.
"""

from __future__ import annotations

import logging
import re
import sys

_LOGGER_NAME = "oneglobal_mcp"

# Patrón para redactar cabeceras Authorization en cualquier texto registrado.
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(token|bearer)\s+\S+")


def mask_iccid(iccid: str | None) -> str | None:
    """Enmascara un ICCID conservando solo 3 dígitos finales."""
    if not iccid:
        return iccid
    return f"{iccid[:6]}…{iccid[-3:]}" if len(iccid) >= 9 else "***"


def mask_imei(imei: str | None) -> str | None:
    """Enmascara un IMEI conservando solo los 2 últimos dígitos."""
    if not imei:
        return imei
    return f"{imei[:4]}…{imei[-2:]}" if len(imei) >= 6 else "***"


def mask_token(token: str | None) -> str:
    """Nunca devuelve un token completo."""
    if not token:
        return "<no configurado>"
    return f"{token[:4]}…<oculto:{len(token)}>"


def redact(text: str, token: str | None = None) -> str:
    """Elimina secretos de un texto antes de registrarlo en logs."""
    result = _AUTH_RE.sub(r"\1<SECRETO_REDACTADO>", text or "")
    if token:
        result = result.replace(token, "<TOKEN_REDACTADO>")
    return result


class SecretRedactionFilter(logging.Filter):
    """FiltRA secretos en TODOS los registros que pasan por él (defensa central).

    Se instala en los loggers del proyecto con el token configurado: aunque el
    llamador olvide redactar, el token y las cabeceras Authorization jamás
    quedan completos en la salida (regla 7 del documento).
    """

    def __init__(self, token: str | None = None) -> None:
        super().__init__()
        self._token = token or ""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = redact(message, self._token)
        record.args = ()
        return True


def configure_logging(level: str = "INFO", *, token: str | None = None) -> logging.Logger:
    """Configura logging raíz del proyecto hacia stderr con nivel dado."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    redaction_filter = SecretRedactionFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction_filter)
    logging.getLogger(_LOGGER_NAME).addFilter(redaction_filter)
    # Silenciar el ruido de librerías para no ensuciar el canal de diagnóstico.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(_LOGGER_NAME)


def get_logger(name: str) -> logging.Logger:
    """Logger hijo con redacción aplicada por el llamador vía ``redact``."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
