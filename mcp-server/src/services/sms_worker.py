"""Worker ligero del ciclo de vida SMS (sondeo y detección de respuestas).

Diseño pensado para un servidor stdio posiblemente efímero:

- El servidor MCP NO depende del worker para ser correcto: ``get_sms_status``
  y ``get_sms_response`` refrescan/detectan bajo demanda.
- El worker corre como tarea asyncio mientras el proceso vive (arranca en el
  lifespan) o de forma persistente con ``python main.py worker``.
- Las pasadas NO se solapan (lock): si una pasada aún corre, la siguiente se
  salta (guía §4.1).

Pasada (guía §2.3 y §3):

1. Recuperación: tareas 'pending' de transmisión real marcadas 'uncertain'
   (el servidor se cortó antes de confirmar el POST: jamás se reenvía a ciegas).
2. Sondeo de tareas 'sent' (con id EXTERNO, dentro de la edad máxima) con
   período de gracia de entrega.
3. Detección de respuestas para tareas entregadas sin respuesta, dentro de la
   ventana de respuesta.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from src.services.sms_repository import SmsRepository
from src.services.sms_service import INTERNAL_UNCERTAIN, SmsService
from src.utils.logging_utils import get_logger
from src.utils.time_utils import iso, utc_now

logger = get_logger("worker")


class SmsWorker:
    """Worker asíncrono anti-solape para el laboratorio de un dispositivo."""

    def __init__(self, settings, service: SmsService, repo: SmsRepository) -> None:
        self._cfg = settings
        self._service = service
        self._repo = repo
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Ejecución
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Arranca el bucle en background (llamado desde el lifespan)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="sms-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_forever(self) -> None:
        interval = max(1.0, self._cfg.sms_poll_interval_seconds)
        while True:
            await self.maybe_run_pass()
            await asyncio.sleep(interval)

    async def maybe_run_pass(self) -> bool:
        """Ejecuta una pasada si no hay otra en curso. Devuelve si se ejecutó."""
        if self._lock.locked():
            return False
        async with self._lock:
            await self.run_pass()
            return True

    # ------------------------------------------------------------------ #
    # Pasada
    # ------------------------------------------------------------------ #
    async def run_pass(self) -> dict[str, int]:
        stats = {"recovered": 0, "polled": 0, "changed": 0, "responses": 0}
        try:
            stats["recovered"] += await self._recover_pending()
            stats["polled"] = await self._poll_sent()
            stats["responses"] = await self._detect_responses()
        except Exception as exc:  # la pasada nunca tumba el worker
            logger.error("Pasada del worker falló: %s", exc)
        return stats

    async def _recover_pending(self) -> int:
        """Tareas 'pending' de transmisión real: se marcan inciertas, sin reenviar."""
        recovered = 0
        pending = await self._repo.tasks_by_status(["pending"], limit=self._cfg.sms_send_per_pass)
        for task in pending:
            if task["mode"] == "dry_run":
                continue  # las dry-run permanecen pendientes para siempre: jamás se transmiten
            await self._repo.update_task(
                task["local_task_id"],
                {
                    "status": INTERNAL_UNCERTAIN,
                    "error_message": (
                        "El servidor se reinició antes de confirmar la transmisión; el envío pudo "
                        "haber llegado a 1GLOBAL. Conciliar manualmente (listado de detalles de SMS): "
                        "NO se reenvió automáticamente."
                    ),
                },
            )
            await self._repo.audit("RECOVERY_MARKED_UNCERTAIN", task["local_task_id"])
            recovered += 1
        return recovered

    async def _poll_sent(self) -> int:
        """Sondea tareas 'sent' jóvenes (backlog mayor ya no se sondea)."""
        cutoff = iso(utc_now() - timedelta(seconds=self._cfg.sms_poll_max_age_seconds))
        inflight = await self._repo.tasks_by_status(
            ["sent"], created_from=cutoff, limit=self._cfg.sms_poll_batch_size
        )
        for task in inflight:
            await self._service.poll_sent_task(task)
        return len(inflight)

    async def _detect_responses(self) -> int:
        """Entregadas recientes sin respuesta: la respuesta puede llegar ~3 min tarde."""
        cutoff = iso(utc_now() - timedelta(seconds=self._cfg.sms_response_window_seconds))
        delivered = await self._repo.tasks_by_status(["delivered"], created_from=cutoff, limit=20)
        checked = 0
        for task in delivered:
            if task.get("response_received"):
                continue
            checked += 1
            await self._service.detect_response_for_delivered(task)
        return checked
