"""Ciclo de vida con worker sobre SQLite real (archivos temporales)."""

from __future__ import annotations

from src.config import load_settings
from src.index import create_components
from src.models import SendSmsInput
from tests.conftest import write_env_file


async def test_ciclo_completo_con_worker(tmp_path) -> None:
    env_path = write_env_file(tmp_path)
    settings = load_settings(env_path)
    components = create_components(settings)
    await components.repo.connect()
    try:
        service = components.service
        worker = components.worker

        # La skill prepara y el usuario confirma: envío en modo replay.
        sent = await service.submit(
            SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="wl-1")
        )
        assert sent.status == "sent"

        # Pasada 1: sondea 'sent' (no cambia aún).
        stats = await worker.run_pass()
        assert stats["polled"] >= 1
        # Pasada 2: el replay confirma entrega.
        await worker.run_pass()
        status = await service.status(sent.local_task_id)
        assert status.status == "delivered"

        # Pasada 3: detecta la respuesta del equipo.
        await worker.run_pass()
        response = await service.response(sent.local_task_id)
        assert response.found is True
        assert response.response_content != "  web_connect"

        # El worker de nuevo ya no repite la detección (respuesta persistida).
        again = await service.response(sent.local_task_id)
        assert again.found is True
        assert again.response_content == response.response_content
    finally:
        await components.worker.stop()
        await components.client.close()
        await components.repo.close()


async def test_respuesta_con_wait_seconds_entrega_en_misma_llamada(tmp_path) -> None:
    """La espera activa (wait_seconds) obtiene la respuesta en la MISMA llamada."""
    import time

    env_path = write_env_file(tmp_path)
    settings = load_settings(env_path)
    components = create_components(settings)
    await components.repo.connect()
    try:
        service, worker = components.service, components.worker
        sent = await service.submit(
            SendSmsInput(target="test", text="  web_connect", dry_run=False, request_id="wl-wait")
        )
        await worker.run_pass()
        await worker.run_pass()  # replay confirma entrega
        status = await service.status(sent.local_task_id)
        assert status.status == "delivered"

        t0 = time.monotonic()
        response = await service.response(sent.local_task_id, wait_seconds=10)
        elapsed = time.monotonic() - t0
        assert response.found is True
        assert response.response_content not in (None, "")
        assert elapsed < 10.0  # la detección corta la espera en cuanto llega

        # Llamada posterior SIN espera: devuelve la respuesta persistida.
        instant = await service.response(sent.local_task_id)
        assert instant.found is True
        assert instant.response_content == response.response_content
    finally:
        await components.worker.stop()
        await components.client.close()
        await components.repo.close()


async def test_wait_seconds_no_se_aplica_a_tareas_sin_transmision_real(tmp_path) -> None:
    """Dry-run: la espera cortocircuita sin escandalizar (no hay que sondear nada)."""
    import time

    env_path = write_env_file(tmp_path)
    settings = load_settings(env_path)
    components = create_components(settings)
    await components.repo.connect()
    try:
        service = components.service
        sent = await service.submit(
            SendSmsInput(target="test", text="  web_connect", dry_run=True, request_id="wl-wait-dry")
        )
        t0 = time.monotonic()
        response = await service.response(sent.local_task_id, wait_seconds=10)
        assert response.found is False
        assert time.monotonic() - t0 < 2.0  # corto-circuito inmediato
    finally:
        await components.worker.stop()
        await components.client.close()
        await components.repo.close()


async def test_worker_recupera_pendiente_como_incierto_sin_reenviar(tmp_path) -> None:
    env_path = write_env_file(tmp_path)
    settings = load_settings(env_path)
    components = create_components(settings)
    await components.repo.connect()
    try:
        service, worker, repo = components.service, components.worker, components.repo
        # Simula una tarea 'pending' de transmisión real dejada por un reinicio.
        task, _created = await repo.create_task(
            {
                "local_task_id": "smsq_reinicio01",
                "request_id": "req-reinicio",
                "iccid": settings.test_iccid,
                "command": "  web_connect",
                "mode": "replay",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        await worker.run_pass()
        after = await repo.get_task(task["local_task_id"])
        assert after["status"] == "uncertain"
        assert "NO se reenvió" in (after["error_message"] or "")
        # Las dry-run NO se marcan inciertas: se quedan pendientes para siempre.
        prepared = await service.submit(SendSmsInput(target="test", command="getinfo"))
        await worker.run_pass()
        still = await repo.get_task(prepared.local_task_id)
        assert still["status"] == "pending"
    finally:
        await components.client.close()
        await components.repo.close()
