"""Integración MCP: descubrimiento e invocación por el SDK oficial (mcp 2.x).

Se usa el cliente en-proceso del SDK (``mcp.Client`` con la instancia del
servidor): sin red, exactamente el mismo protocolo que un cliente real.
"""

from __future__ import annotations

import pytest
from mcp import Client

from src.index import create_app
from tests.conftest import parse_tool_result, write_env_file


@pytest.fixture()
async def app(tmp_path):
    env_path = write_env_file(tmp_path)
    server, components = create_app(env_path)
    yield server, components
    # El lifespan abre/cierra la BD; para consultas directas cerramos aquí.
    try:
        await components.repo.close()
    except RuntimeError:
        pass


async def test_herramientas_descubribles_con_descripcion_espanol(app) -> None:
    server, _components = app
    async with Client(server) as client:
        listing = await client.list_tools()
    tools = listing.tools
    names = {tool.name for tool in tools}
    assert names == {"send_sms", "get_sms_status", "get_sms_response"}
    for tool in tools:
        assert tool.description and tool.description.strip()
        assert tool.description[0].isupper()


async def test_send_sms_dry_run_por_mcp(app) -> None:
    server, _components = app
    async with Client(server) as client:
        result = await client.call_tool(
            "send_sms",
            {"target": "test", "command": "web_connect", "dry_run": True},
        )
        data = parse_tool_result(result)
    assert data["success"] is True
    assert data["mode"] == "dry_run"
    assert data["status"] == "simulated"
    assert data["local_task_id"]
    assert "No se realizó ninguna transmisión" in data["message"]


async def test_send_sms_rechaza_destino_desconocido(app) -> None:
    server, _components = app
    async with Client(server) as client:
        result = await client.call_tool(
            "send_sms", {"target": "8944474400009999999", "text": "  web_connect", "dry_run": True}
        )
        data = parse_tool_result(result)
    assert data["success"] is False
    assert data["status"] == "rejected"
    assert "no autorizado" in data["message"]


async def test_get_sms_status_tarea_inexistente(app) -> None:
    server, _components = app
    async with Client(server) as client:
        result = await client.call_tool("get_sms_status", {"local_task_id": "smsq_nadie"})
        data = parse_tool_result(result)
    assert data["success"] is False
    assert "No existe" in data["message"]


async def test_alias_reference_id_de_la_skill(app) -> None:
    server, components = app
    from src.models import SendSmsInput

    async with Client(server) as client:
        # Prepara una tarea dry-run mientras el lifespan mantiene la BD abierta.
        prepared = await components.service.submit(
            SendSmsInput(target="test", command="getinfo", dry_run=True)
        )
        result = await client.call_tool(
            "get_sms_status", {"reference_id": prepared.local_task_id}
        )
        data = parse_tool_result(result)
    assert data["success"] is True
    assert data["local_task_id"] == prepared.local_task_id


async def test_ciclo_completo_estilo_skill(app) -> None:
    """El flujo exigido en §14: skill identifica web_connect → MCP envía →
    1GLOBAL replay acepta/entrega → MCP detecta la respuesta."""
    server, _components = app
    async with Client(server) as client:
        # La skill prepara el SMS con DOS espacios iniciales (sin credenciales).
        sent = parse_tool_result(
            await client.call_tool(
                "send_sms",
                {
                    "target": "test",
                    "text": "  web_connect",
                    "dry_run": False,
                    "request_id": "lifecycle-skill-1",
                },
            )
        )
        assert sent["success"] is True
        assert sent["status"] == "sent"
        local_id = sent["local_task_id"]

        # Seguimiento: 1ª consulta 'sent', 2ª 'delivered'.
        first = parse_tool_result(await client.call_tool("get_sms_status", {"local_task_id": local_id}))
        assert first["status"] == "sent"
        delivered = parse_tool_result(
            await client.call_tool("get_sms_status", {"local_task_id": local_id})
        )
        assert delivered["status"] == "delivered"

        # Pregunta final: "¿Qué respondió el dispositivo?"
        response = parse_tool_result(
            await client.call_tool("get_sms_response", {"local_task_id": local_id})
        )
        assert response["found"] is True
        assert response["response_content"]
        assert response["response_content"] != "  web_connect"
        assert "FOTA WEB" in response["response_content"]
