"""Aprobación de transmisión real: token de un solo uso controlado por el OPERADOR.

Un booleano generado por el LLM (p. ej. ``confirmed: true``) NO es prueba de
autorización. Este mecanismo exige un token HMAC firmado con un secreto que
solo existe en el servidor (archivo 0600 en ``data/``), emitido por el
operador con el CLI (``python main.py mint``) y enlazado a:

- el ICCID autorizado de pruebas,
- el hash SHA-256 de la carga útil exacta,
- una expiración (de 15 minutos por defecto),
- UNA SOLA USO (el jti se marca como consumido en SQLite).

El token nunca se muestra en los outputs de las herramientas MCP; el agente
solo solicita al operador generarlo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any


class ApprovalError(Exception):
    """Token de aprobación inválido, expirado, ajeno o ya consumido."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def load_or_create_secret(path: str | Path) -> bytes:
    """Carga el secreto HMAC local (32 bytes hex) o lo crea con permisos 0600."""
    secret_path = Path(path)
    if not secret_path.exists():
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_bytes(os.urandom(32).hex().encode("ascii"))
        try:
            secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # p. ej. en algunos sistemas de archivos remotos
            pass
    secret = secret_path.read_bytes().strip()
    if len(secret) < 32:
        raise ApprovalError("El secreto de aprobación local es inválido (demasiado corto).")
    return secret


class ApprovalManager:
    """Emite/verifica tokens de aprobación de un solo uso (HMAC-SHA256)."""

    def __init__(self, secret: bytes, ttl_seconds: int = 900) -> None:
        if ttl_seconds <= 0:
            raise ValueError("La vigencia del token debe ser positiva.")
        self._secret = secret
        self._ttl = ttl_seconds

    def mint(self, *, iccid: str, payload: str, ttl_seconds: int | None = None) -> str:
        """Genera un token nuevo para el ICCID + carga exactos."""
        ttl = ttl_seconds or self._ttl
        body = {
            "jti": uuid.uuid4().hex,
            "iccid": iccid,
            "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "exp": time.time() + ttl,
        }
        blob = base64.urlsafe_b64encode(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(self._secret, blob, hashlib.sha256).hexdigest()
        return f"{blob.decode('ascii')}.{signature}"

    def verify(self, token: str | None, *, iccid: str, payload: str, now: float | None = None) -> str:
        """Verifica firma, expiración y enlazado ICCID/carga; devuelve el jti."""
        if not token or "." not in token:
            raise ApprovalError(
                "Falta el token de aprobación de un solo uso: pide al operador del servidor "
                "generarlo con 'python main.py mint' (nunca lo genera el modelo)."
            )
        blob_raw, _, signature = token.partition(".")
        try:
            blob = blob_raw.encode("ascii")
        except UnicodeError:
            raise ApprovalError("El token de aprobación tiene un formato inválido.") from None
        expected = hmac.new(self._secret, blob, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ApprovalError("El token de aprobación no es válido (firma incorrecta).")
        body: dict[str, Any] = json.loads(base64.urlsafe_b64decode(blob))
        if body.get("iccid") != iccid:
            raise ApprovalError("El token de aprobación fue emitido para otro dispositivo.")
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if body.get("payload_sha256") != payload_hash:
            raise ApprovalError(
                "El token de aprobación corresponde a OTRA carga útil; solicita uno nuevo con el "
                "comando exacto a transmitir."
            )
        if (now if now is not None else time.time()) > float(body.get("exp", 0)):
            raise ApprovalError("El token de aprobación expiró; pide uno nuevo con 'python main.py mint'.")
        return str(body["jti"])
