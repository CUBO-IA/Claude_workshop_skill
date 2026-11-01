"""Cliente 1GLOBAL live bajo respx: cabeceras, rutas v2.0, reintentos y POST dudoso.

No hay llamadas de red reales: respx intercepta httpx.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from src.config import Settings
from src.providers.oneglobal_client import (
    LiveProvider,
    OneGlobalClient,
    ProviderError,
    UncertainSubmission,
)

BASE = "https://iot.truphone.com/api"
TOKEN = "token-de-pruebas-123"


def live_settings() -> Settings:
    return Settings(
        oneglobal_mode="live",
        oneglobal_token=TOKEN,
        oneglobal_test_iccid="8944474400001005750",
        teltonika_test_imei="356307042441014",
        http_max_retries=3,
        oneglobal_rate_limit_per_minute=600,
    )


@respx.mock
async def test_cabecera_token_y_payload_exacto() -> None:
    route = respx.post(f"{BASE}/v2.0/sims/send_sms/").mock(
        return_value=Response(200, json={"resourceURL": "https://x/api/v2.0/sims/send_sms//status/id1/"})
    )
    provider = LiveProvider(live_settings())
    try:
        result = await provider.submit_sms(
            iccid="8944474400001005750", text_payload="  web_connect", request_id="r1"
        )
    finally:
        await provider.close()
    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Token {TOKEN}"
    body = request.read().decode()
    assert '"iccid": ["8944474400001005750"]' in body or '"iccid":["8944474400001005750"]' in body
    # El texto se conserva EXACTO, incluidos los espacios iniciales.
    assert '"  web_connect"' in body
    assert result.external_task_id == "id1"


@respx.mock
async def test_endpoint_estado_es_v2_0() -> None:
    route = respx.get(f"{BASE}/v2.0/sims/send_sms/status/id1/").mock(
        return_value=Response(
            200,
            json={
                "id": "id1",
                "deliveryReport": [{"iccid": "8", "deliveryStatus": "DELIVERED"}],
            },
        )
    )
    provider = LiveProvider(live_settings())
    try:
        status = await provider.get_status("id1")
    finally:
        await provider.close()
    assert route.called
    assert "/v2.0/sims/send_sms/status/id1/" in str(route.calls.last.request.url)
    assert status.status == "delivered"


@respx.mock
async def test_get_reintenta_500_y_luego_200() -> None:
    route = respx.get(f"{BASE}/v2.0/sims/send_sms/status/id1/").mock(
        side_effect=[Response(500, json={"error": "x"}), Response(200, json={"deliveryReport": []})]
    )
    provider = LiveProvider(live_settings())
    try:
        status = await provider.get_status("id1")
    finally:
        await provider.close()
    assert route.call_count == 2
    assert status.status == "unknown"  # sin reporte NO es entregado


@respx.mock
async def test_4xx_no_reintenta() -> None:
    route = respx.get(f"{BASE}/v2.0/sims/send_sms/status/id1/").mock(
        return_value=Response(404, json={"detail": "no encontrado"})
    )
    provider = LiveProvider(live_settings())
    try:
        with pytest.raises(ProviderError, match="404"):
            await provider.get_status("id1")
    finally:
        await provider.close()
    assert route.call_count == 1


@respx.mock
async def test_post_timeout_no_se_reintenta() -> None:
    route = respx.post(f"{BASE}/v2.0/sims/send_sms/").mock(side_effect=httpx.TimeoutException("timeout"))
    provider = LiveProvider(live_settings())
    try:
        with pytest.raises(UncertainSubmission):
            await provider.submit_sms(iccid="8", text_payload="  web_connect", request_id="r2")
    finally:
        await provider.close()
    assert route.call_count == 1  # NUNCA se repite un POST dudoso


@respx.mock
async def test_post_504_es_incierto() -> None:
    respx.post(f"{BASE}/v2.0/sims/send_sms/").mock(return_value=Response(504, json={"detail": "gw"}))
    provider = LiveProvider(live_settings())
    try:
        with pytest.raises(UncertainSubmission):
            await provider.submit_sms(iccid="8", text_payload="  web_connect", request_id="r3")
    finally:
        await provider.close()


@respx.mock
async def test_post_rechazo_4xx_es_provider_error() -> None:
    respx.post(f"{BASE}/v2.0/sims/send_sms/").mock(
        return_value=Response(400, json={"detail": "iccid inválido"})
    )
    provider = LiveProvider(live_settings())
    try:
        with pytest.raises(ProviderError, match="400"):
            await provider.submit_sms(iccid="8", text_payload="  web_connect", request_id="r4")
    finally:
        await provider.close()


@respx.mock
async def test_listado_detalles_con_filtros_correctos() -> None:
    route = respx.get(f"{BASE}/v2.0/sims/send_sms/status/").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"iccid": "8", "content": "  web_connect", "dateSubmitted": "2026-11-01 14:02:11"},
                    {"iccid": "8", "content": "respuesta", "startDate": "2026-11-01 14:05:30"},
                ]
            },
        )
    )
    from datetime import UTC, datetime

    provider = LiveProvider(live_settings())
    try:
        details = await provider.list_details(
            date_from=datetime(2026, 11, 1, 13, 52, 11, tzinfo=UTC), iccid="8"
        )
    finally:
        await provider.close()
    assert len(details) == 2
    sent_url = route.calls.last.request.url
    assert sent_url.params["dateSubmitted_from"] == "2026-11-01 13:52:11"  # 'T'→espacio, sin Z
    assert sent_url.params["ordering"] == "-dateSubmitted"
    assert sent_url.params["iccid"] == "8"


def test_live_provider_sin_token_no_arranca() -> None:
    settings = live_settings()
    settings.oneglobal_token = ""  # vaciado tras construir: LiveProvider lo exige
    with pytest.raises(ProviderError, match="GLOBAL_API"):
        LiveProvider(settings)


def test_cliente_replay_usa_fixtures(tmp_path) -> None:
    settings = Settings(fixtures_dir="tests/fixtures")
    client = OneGlobalClient(settings)
    assert client.mode == "replay"
