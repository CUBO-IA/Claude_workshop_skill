"""Limitador de peticiones a 1GLOBAL (depósito de tokens, sin ráfagas).

Por defecto 100 peticiones/minuto: límite conservador del cliente que demostró
ser prudente para producir lotes grandes; los límites reales del portal son
desconocidos. En este laboratorio de un solo dispositivo, el ritmo estricto
también protege contra bucles de sondeo descontrolados.
"""

from __future__ import annotations

import asyncio
import time


class AsyncTokenLimiter:
    """Depósito de tokens con capacidad 1: estriñe el ritmo a ``per_minute``/60 p/s."""

    def __init__(self, per_minute: int) -> None:
        if per_minute <= 0:
            raise ValueError("El límite por minuto debe ser positivo.")
        self._rate = per_minute / 60.0
        self._tokens = 1.0
        self._updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(1.0, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> None:
        """Espera (si hace falta) hasta poder consumir un token."""
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            needed = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(max(needed, 0.005))
