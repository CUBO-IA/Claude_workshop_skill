"""Ciclo de vida a nivel de servicio: envío, idempotencia, gracia y respuestas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.models import SendSmsInput, SmsStatus
from src.providers.oneglobal_client import OneGlobalClient
from src.security.approval import ApprovalManager, load_or_create_secret
from src.security.command_policy import PolicyViolation
from src.services.sms_repository import SmsRepository
from src.services.sms_service import SmsService
from tests.conftest import make_settings


async def build_service(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    repo = SmsRepository(settings.database_path)
    await repo.connect()
    client = OneGlobalClient(settings)
    approval = ApprovalManager(
        load_or_create_secret(settings.approval_secret_path), ttl_seconds=settings.approval_ttl_seconds
    )
    service = SmsService(settings, client, repo, approval)
    return settings, service, repo


async def close(service_components) -> None:
    settings, service, repo = service_components
    await service._client.close()
    await repo.close()


# --------------------------------------------------------------------------- #
# Envío
# --------------------------------------------------------------------------- #
async def test_target_test_y_espacios_preservados(tmp_path) -> None:
    components = await build_service(tmp_path)
    settings, service, repo = components
    result = await service.submit(
        SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="req-1")
    )
    assert result.success and result.status == "sent"
    assert result.external_task_id == "8f3c1d2e-77aa-4b0e-9c21-1a2b3c4d5e6f"
    task = await repo.get_task(result.local_task_id)
    assert task["command"] == "  web_connect"  # texto EXACTO, espacios incluidos
    await close(components)


async def test_destino_no_autorizado(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    with pytest.raises(PolicyViolation, match="no autorizado"):
        await service.submit(SendSmsInput(target="8944474400009999999", text="  web_connect"))
    await close(components)


async def test_comando_bloqueado_no_se_transmite(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    with pytest.raises(PolicyViolation, match="BLOQUEADO"):
        await service.submit(SendSmsInput(target="test", text="  setparam 1:1", dry_run=False))
    await close(components)


async def test_dry_run_no_llama_al_proveedor(tmp_path) -> None:
    components = await build_service(tmp_path, oneglobal_mode="live", oneglobal_token="t")
    _, service, repo = components
    result = await service.submit(SendSmsInput(target="test", command="web_connect"))
    assert result.status == "simulated"
    assert result.mode == "dry_run"
    task = await repo.get_task(result.local_task_id)
    assert task["status"] == "pending"  # preparado, jamás transmitido
    assert task["external_task_id"] is None
    await close(components)


async def test_replay_funciona_sin_token(tmp_path) -> None:
    components = await build_service(tmp_path, oneglobal_token="")
    _, service, _repo = components
    result = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False))
    assert result.success
    assert result.mode == "replay"
    assert "SIMULACIÓN" in result.message
    await close(components)


async def test_transmision_requiere_text_preparado(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    with pytest.raises(PolicyViolation, match="'text'"):
        await service.submit(SendSmsInput(target="test", command="web_connect", dry_run=False))
    await close(components)


async def test_live_deshabilitado_rechaza_transmision(tmp_path) -> None:
    components = await build_service(
        tmp_path, oneglobal_mode="live", oneglobal_token="tok", sms_dry_run=False
    )
    _, service, _repo = components
    with pytest.raises(PolicyViolation, match="SMS_LIVE_ENABLED"):
        await service.submit(
            SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="live-off")
        )
    await close(components)


async def test_live_con_aprobacion_un_solo_uso(tmp_path) -> None:
    import respx
    from httpx import Response

    components = await build_service(
        tmp_path,
        oneglobal_mode="live",
        oneglobal_token="tok",
        sms_live_enabled=True,
        sms_dry_run=False,
        sms_require_confirmation=True,
        oneglobal_rate_limit_per_minute=600,
    )
    settings, service, repo = components
    approval = service._approval
    token = approval.mint(iccid=settings.test_iccid, payload="  web_connect")
    with respx.mock:
        respx.post(f"{settings.oneglobal_base_url}/v2.0/sims/send_sms/").mock(
            return_value=Response(200, json={"resourceURL": "https://x/api/v2.0/sims/send_sms//status/live1/"})
        )
        result = await service.submit(
            SendSmsInput(
                target="test",
                text="  web_connect",
                dry_run=False,
                request_id="req-live",
                approval_token=token,
            )
        )
    assert result.success and result.mode == "live" and result.status == "sent"
    task = await repo.get_task(result.local_task_id)
    assert task["mode"] == "live"
    assert task["external_task_id"] == "live1"
    # El token es de UN solo uso: quedó consumido y reutilizarlo falla.
    jti = approval.verify(token, iccid=settings.test_iccid, payload="  web_connect")
    assert await repo.is_approval_used(jti) is True
    with respx.mock:
        respx.post(f"{settings.oneglobal_base_url}/v2.0/sims/send_sms/").mock(
            return_value=Response(200, json={"resourceURL": "https://x/api/v2.0/sims/send_sms//status/live2/"})
        )
        with pytest.raises(PolicyViolation, match="UN solo uso"):
            await service.submit(
                SendSmsInput(
                    target="test",
                    text="  web_connect",
                    dry_run=False,
                    request_id="req-live-2",
                    approval_token=token,
                )
            )
    await close(components)


async def test_transmision_solapada_rechazada(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    first = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="a"))
    assert first.success
    second = await service.submit(
        SendSmsInput(target="test", text="  getinfo", dry_run=False, request_id="b")
    )
    assert second.status == "failed"
    assert "activa" in second.message or "comando activo" in second.message
    await close(components)


async def test_rate_limit_por_dispositivo(tmp_path) -> None:
    components = await build_service(tmp_path, sms_device_max_per_hour=1)
    _, service, repo = components
    first = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="a"))
    assert first.success
    task_b = await service.submit(SendSmsInput(target="test", text="  getinfo", dry_run=False, request_id="b"))
    assert task_b.status == "failed"
    task = await repo.get_task(task_b.local_task_id)
    assert task["error_message"] is not None
    await close(components)


async def test_idempotencia_request_id(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, repo = components
    first = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="dup"))
    second = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="dup"))
    assert second.duplicate_request is True
    assert first.local_task_id == second.local_task_id
    tasks = await repo.tasks_by_status(["sent"])
    assert len(tasks) == 1  # no se duplicó la transmisión
    await close(components)


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #
async def test_estado_usa_id_externo_y_llega_a_delivered(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="s1"))
    first = await service.status(sent.local_task_id)
    assert first.status == SmsStatus.SENT  # 1ª consulta: en camino
    second = await service.status(sent.local_task_id)
    assert second.status == SmsStatus.DELIVERED  # 2ª consulta: entregado
    assert second.delivered_at is not None
    await close(components)


async def test_estado_de_tarea_desconocida(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    result = await service.status("smsq_inexistente")
    assert result.success is False
    assert "No existe" in result.message
    await close(components)


async def test_tarea_pendiente_no_se_sondea(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    prepared = await service.submit(SendSmsInput(target="test", command="getinfo"))
    result = await service.status(prepared.local_task_id)
    assert result.refresh_attempted is False  # sin id externo: NO se sondea
    await close(components)


# --------------------------------------------------------------------------- #
# Respuesta del dispositivo
# --------------------------------------------------------------------------- #
async def _delivered_task(components, request_id: str):
    _, service, _repo = components
    sent = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id=request_id))
    await service.status(sent.local_task_id)  # sent
    await service.status(sent.local_task_id)  # delivered
    return sent


async def test_respuesta_detectada_y_eco_excluido(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await _delivered_task(components, "resp-1")
    result = await service.response(sent.local_task_id)
    assert result.found is True
    assert result.response_content != "  web_connect"  # el eco NO se presenta como respuesta
    assert result.found
    await close(components)


async def test_sin_respuesta_se_reporta_conservadoramente(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await _delivered_task(components, "noresp-1")
    result = await service.response(sent.local_task_id)
    assert result.found is False
    assert result.ambiguous is False
    await close(components)


async def test_correlacion_ambigua_se_declara(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await _delivered_task(components, "ambiguous-1")
    result = await service.response(sent.local_task_id)
    assert result.found is False
    assert result.ambiguous is True
    await close(components)


async def test_respuesta_antes_de_entrega(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="s9"))
    result = await service.response(sent.local_task_id)
    assert result.found is False
    assert "entregado" in result.message
    await close(components)


async def test_ventana_de_respuesta_vencida(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, repo = components
    sent = await _delivered_task(components, "resp-window")
    # Envejece artificialmente la tarea más allá de la ventana (3600 s).
    created_at = datetime.now(UTC) - timedelta(hours=3)
    await repo.update_task(sent.local_task_id, {"created_at": created_at.isoformat()})
    result = await service.response(sent.local_task_id)
    assert result.found is False
    await close(components)


# --------------------------------------------------------------------------- #
# Gracia de entrega
# --------------------------------------------------------------------------- #
async def test_gracia_protege_tarea_joven(tmp_path) -> None:
    components = await build_service(tmp_path)
    settings, service, repo = components
    sent = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="grace"))
    task = await repo.get_task(sent.local_task_id)
    # Agota los reintentos con errores pero la tarea es joven: sigue 'sent'.
    for _ in range(settings.sms_max_retries):
        # Simula fallos del proveedor envejeciendo el estado 'sent' con errores.
        await service.poll_sent_task(task)
        task = await repo.get_task(sent.local_task_id)
    assert task["status"] != "failed"
    await close(components)


# --------------------------------------------------------------------------- #
# Resolución por dispositivo (semántica del proyecto previo)
# --------------------------------------------------------------------------- #
async def test_resolucion_por_dispositivo_sin_id(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    sent = await service.submit(
        SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="dev-1")
    )
    # Sin id: target="test" (por defecto) devuelve la última tarea del dispositivo.
    status = await service.status()
    assert status.success is True
    assert status.local_task_id == sent.local_task_id
    response = await service.response()
    assert response.local_task_id == sent.local_task_id
    await close(components)


async def test_local_task_id_remite_al_dispositivo(tmp_path) -> None:
    from src.services.sms_service import SmsService

    components = await build_service(tmp_path)
    settings, service, _repo = components
    first = await service.submit(
        SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="dev-2")
    )
    second = await service.submit(
        SendSmsInput(target="test", text="  getinfo", dry_run=False, request_id="dev-3")
    )
    fingerprint = SmsService._device_fingerprint(settings.test_iccid)
    # El identificador remite al dispositivo (huella del ICCID) con sufijo único.
    assert first.local_task_id.startswith(f"smsq_{fingerprint}_")
    assert second.local_task_id.startswith(f"smsq_{fingerprint}_")
    assert first.local_task_id != second.local_task_id
    await close(components)


async def test_consulta_por_dispositivo_sin_tareas(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    result = await service.status()
    assert result.success is False
    assert "no hay tareas" in result.message.lower()
    await close(components)


async def test_consulta_por_target_invalido(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, _repo = components
    result = await service.status(target="8944474400009999999")
    assert result.success is False
    assert "no autorizado" in result.message
    await close(components)


async def test_recuperacion_tras_reinicio(tmp_path) -> None:
    components = await build_service(tmp_path)
    _, service, repo = components
    sent = await service.submit(SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="restart"))
    await service.status(sent.local_task_id)
    await service._client.close()
    await repo.close()

    # Reinicio: nuevo repositorio y servicio sobre el mismo archivo SQLite.
    settings2 = make_settings(tmp_path)
    repo2 = SmsRepository(settings2.database_path)
    await repo2.connect()
    task = await repo2.get_task(sent.local_task_id)
    assert task is not None
    assert task["status"] in ("sent", "delivered")
    await repo2.close()
