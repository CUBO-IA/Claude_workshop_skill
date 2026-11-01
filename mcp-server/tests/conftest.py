"""Utilidades compartidas de pruebas: configuración temporal y clientes MCP.

Las pruebas NUNCA hacen llamadas de red reales: el modo replay funciona sin
token y el transporte live solo se ejercita con respx sobre URLs falsificadas.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Settings

TEST_IMEI = "356307042441014"
TEST_ICCID = "8944474400001005750"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings válidas de laboratorio apuntando a archivos temporales."""
    values: dict = {
        "oneglobal_mode": "replay",
        "teltonika_test_imei": TEST_IMEI,
        "oneglobal_test_iccid": TEST_ICCID,
        "database_path": str(tmp_path / "sms_tasks.db"),
        "approval_secret_path": str(tmp_path / "approval.key"),
        "fixtures_dir": str(Path(__file__).parent / "fixtures"),
        "sms_poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return Settings(**values)


def write_env_file(tmp_path: Path, **overrides: str) -> str:
    """Escribe un .env temporal y devuelve su ruta (usa el loader real)."""
    values: dict[str, str] = {
        "ONEGLOBAL_MODE": "replay",
        "TELTONIKA_TEST_IMEI": TEST_IMEI,
        "ONEGLOBAL_TEST_ICCID": TEST_ICCID,
        "DATABASE_PATH": str(tmp_path / "sms_tasks.db"),
        "APPROVAL_SECRET_PATH": str(tmp_path / "approval.key"),
        "FIXTURES_DIR": str(Path(__file__).parent / "fixtures"),
        "SMS_POLL_INTERVAL_SECONDS": "0.01",
    }
    values.update(overrides)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    return str(env_path)


def parse_tool_result(result) -> dict:
    """Extrae el payload JSON de una respuesta call_tool (SDK 2.x, tolerante)."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        inner = structured.get("result")
        if isinstance(inner, dict):
            return inner
        return structured
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"No se pudo leer el resultado de la herramienta: {result!r}")


@pytest.fixture()
def anyio_backend():
    return "asyncio"
