"""Fechas UTC para persistencia y para la API de 1GLOBAL.

Formato exigido por el filtro ``dateSubmitted_from``: ``YYYY-MM-DD HH:mm:ss``
(espacio en lugar de 'T', SIN 'Z' y sin milisegundos). Con 'T' la API filtra
mal o devuelve error (guía, §3.1).
"""

from __future__ import annotations

from datetime import UTC, datetime

_API_FORMAT = "%Y-%m-%d %H:%M:%S"
_PARSE_FORMATS = (
    "%Y-%m-%d %H:%M:%S%z",  # '2026-10-31 14:02:11+00:00'
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)


def utc_now() -> datetime:
    """Momento actual en UTC (aware)."""
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    """ISO-8601 UTC con segundos, para almacenar y comparar lexicográficamente."""
    value = dt or utc_now()
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """Parsea una fecha ISO propia (UTC asumido si es naive); None si es inválida."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def format_api_date(value: datetime) -> str:
    """Fecha en el formato del filtro de la API: 'YYYY-MM-DD HH:mm:ss' (UTC)."""
    return value.astimezone(UTC).strftime(_API_FORMAT)


def format_api_date_from_iso(value: str | None) -> str:
    """Convierte ISO propio al formato del filtro; '' si no hay fecha."""
    parsed = parse_iso(value)
    return format_api_date(parsed) if parsed else ""


def parse_api_date(value: str | None) -> datetime | None:
    """Parsea fechas de 1GLOBAL (tolerante: con/sin zona, 'T', milisegundos)."""
    if not value:
        return None
    candidate = value.strip()
    for fmt in _PARSE_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None
