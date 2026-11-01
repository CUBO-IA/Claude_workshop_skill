"""Configuración central del servidor MCP de 1GLOBAL.

Las variables se leen del archivo ``.env`` local (excluido de Git) y se validan
con Pydantic. La configuración se carga una sola vez durante la inicialización
(``load_settings``) y después se consulta con ``get_settings``.

Reglas de seguridad implementadas aquí:

- El modo ``replay`` no exige token de API y nunca realiza llamadas HTTP.
- El modo ``live`` exige un token no vacío y un ICCID de pruebas autorizado.
- IMEI e ICCID nunca se escriben en el código: solo se leen del ``.env``.
- Los identificadores se validan por longitud y formato numérico; eso NO
  demuestra que existan en 1GLOBAL.
- El token nunca se devuelve completo ni en logs (ver ``safe_summary``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ICCID: 19–20 dígitos que empiezan por 89 (formato ITU-T E.118); la válidación
# es de formato, no prueba existencia en 1GLOBAL.
ICCID_RE = re.compile(r"^89\d{17,18}$")
# IMEI: 14 o 15 dígitos (la llave Device de 1GLOBAL son 14 = IMEI sin el último dígito).
IMEI_RE = re.compile(r"^\d{14,15}$")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class Settings(BaseSettings):
    """Variables de entorno del servidor, validadas por Pydantic."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        populate_by_name=True,
    )

    # --- 1GLOBAL API ---
    oneglobal_base_url: str = "https://iot.truphone.com/api"
    # El token se lee de GLOBAL_API (naming real del proyecto);
    # ONEGLOBAL_TOKEN queda como alias de compatibilidad con el documento original.
    oneglobal_token: str = Field(
        "",  # secreto: nunca se loguea completo
        validation_alias=AliasChoices("GLOBAL_API", "ONEGLOBAL_TOKEN"),
    )
    oneglobal_auth_scheme: str = "Token"
    oneglobal_mode: Literal["replay", "live"] = "replay"
    fixtures_dir: str = "./tests/fixtures"

    # --- Dispositivo Teltonika de pruebas ---
    teltonika_test_imei: str = ""
    oneglobal_test_iccid: str = ""

    # --- MCP ---
    mcp_server_name: str = "oneglobal-sms"
    mcp_transport: Literal["stdio"] = "stdio"

    # --- Ejecución SMS ---
    sms_dry_run: bool = True
    sms_live_enabled: bool = False
    sms_require_confirmation: bool = True

    # --- HTTP ---
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    oneglobal_rate_limit_per_minute: int = 100

    # --- Sondeo SMS ---
    sms_poll_interval_seconds: float = 5.0
    sms_sent_grace_seconds: int = 600
    sms_response_window_seconds: int = 3600
    sms_poll_max_age_seconds: int = 86400
    sms_max_retries: int = 3
    sms_device_max_per_hour: int = 5
    sms_send_per_pass: int = 10
    sms_poll_batch_size: int = 10
    response_lookback_seconds: int = 600

    # --- Persistencia y secretos locales ---
    database_path: str = "./data/sms_tasks.db"
    approval_secret_path: str = "./data/approval_secret.key"
    approval_ttl_seconds: int = 900

    # --- Registro ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ------------------------------------------------------------------ #
    # Validadores
    # ------------------------------------------------------------------ #
    @field_validator("oneglobal_base_url")
    @classmethod
    def _clean_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("ONEGLOBAL_BASE_URL no puede estar vacío.")
        return value

    @field_validator("oneglobal_auth_scheme")
    @classmethod
    def _scheme_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ONEGLOBAL_AUTH_SCHEME no puede estar vacío (por defecto 'Token').")
        return value

    @field_validator("teltonika_test_imei")
    @classmethod
    def _imei_format(cls, value: str) -> str:
        value = value.strip()
        if value and not IMEI_RE.fullmatch(value):
            raise ValueError("TELTONIKA_TEST_IMEI debe contener 14 o 15 dígitos numéricos.")
        return value

    @field_validator("oneglobal_test_iccid")
    @classmethod
    def _iccid_format(cls, value: str) -> str:
        value = value.strip()
        if value and not ICCID_RE.fullmatch(value):
            raise ValueError("ONEGLOBAL_TEST_ICCID debe ser un ICCID de 19–20 dígitos que empiece por '89'.")
        return value

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> Settings:
        if self.oneglobal_mode == "live":
            # El modo live NO puede arrancar sin credenciales reales.
            if not self.oneglobal_token:
                raise ValueError(
                    "El modo live exige un token de API (GLOBAL_API). "
                    "El modo replay funciona sin token."
                )
            if not self.oneglobal_test_iccid:
                raise ValueError(
                    "El modo live exige un ICCID de pruebas autorizado (ONEGLOBAL_TEST_ICCID)."
                )
        # En modo replay el token NO es necesario (regla 1 del documento).
        return self

    # ------------------------------------------------------------------ #
    # Derivados seguros
    # ------------------------------------------------------------------ #
    @property
    def test_iccid(self) -> str:
        """ICCID autorizado del dispositivo de pruebas (cadena vacía si falta)."""
        return self.oneglobal_test_iccid

    @property
    def device_key_from_imei(self) -> str | None:
        """Llave Device de 14 dígitos (IMEI sin el dígito verificador), si está configurada."""
        if not self.teltonika_test_imei:
            return None
        return self.teltonika_test_imei[:14]

    @property
    def safe_summary(self) -> dict[str, str]:
        """Resumen seguro para logs: jamás incluye el token completo."""
        token = self.oneglobal_token

        def hidden(value: str) -> str:
            return f"<oculto:{len(value)}>" if value else "<no configurado>"

        return {
            "base_url": self.oneglobal_base_url,
            "mode": self.oneglobal_mode,
            "auth_scheme": self.oneglobal_auth_scheme,
            "token": hidden(token),
            "test_iccid": hidden(self.oneglobal_test_iccid),
            "test_imei": hidden(self.teltonika_test_imei),
            "sms_dry_run": str(self.sms_dry_run),
            "sms_live_enabled": str(self.sms_live_enabled),
        }


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Carga la configuración UNA VEZ. Es la única forma admitida de inicializarla."""
    global _settings
    _settings = Settings(_env_file=str(env_file) if env_file else None)
    return _settings


def get_settings() -> Settings:
    """Devuelve la configuración ya cargada (se inicializó con ``load_settings``)."""
    if _settings is None:
        raise RuntimeError(
            "La configuración no se ha cargado: llama a load_settings() durante la inicialización."
        )
    return _settings


_settings: Settings | None = None
