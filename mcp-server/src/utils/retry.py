"""Política de reintentos HTTP según `guide_api_global.md`.

- Reintentable solo ante 408/425/429/500/502/503/504 o fallo de red/timeout.
- Los 4xx ordinarios NO reintentan.
- Backoff exponencial: base 300 ms, factor 2, techo 4000 ms, con jitter aleatorio.

MODIFICACIÓN OPERATIVA IMPORTANTE (documento de desarrollo, §8): la sumisión de
SMS tiene efectos secundarios. Un POST con timeout pudo haber sido ACEPTADO por
1GLOBAL; por tanto los POST no se reintentan automáticamente: se graban como
sumisión incierta (ver providers.oneglobal_client.UncertainSubmission) y se
concilian manualmente.
"""

from __future__ import annotations

import random

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def is_retryable_status(status: int) -> bool:
    """true solo para los estados que la guía considera reintentables (GET seguros)."""
    return status in RETRYABLE_STATUS


def backoff_delay(attempt: int, base: float = 0.3, cap: float = 4.0) -> float:
    """Retardo del intento ``attempt`` (1-indexado): 300 ms × 2^(n-1), con jitter."""
    raw = min(base * (2 ** max(0, attempt - 1)), cap)
    return raw * (0.5 + random.random() * 0.5)
