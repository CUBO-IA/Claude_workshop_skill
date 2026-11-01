"""Caso 1 del documento: validación de la configuración de entorno."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings, load_settings
from tests.conftest import TEST_ICCID, TEST_IMEI, make_settings, write_env_file


def test_replay_no_necesita_token(tmp_path) -> None:
    settings = make_settings(tmp_path, oneglobal_token="")
    assert settings.oneglobal_mode == "replay"
    assert settings.oneglobal_token == ""  # el modo replay funciona sin token


def test_live_exige_token(tmp_path) -> None:
    with pytest.raises(ValidationError, match="GLOBAL_API"):
        make_settings(tmp_path, oneglobal_mode="live", oneglobal_token="")


def test_live_exige_iccid_de_pruebas(tmp_path) -> None:
    with pytest.raises(ValidationError, match="ONEGLOBAL_TEST_ICCID"):
        make_settings(
            tmp_path,
            oneglobal_mode="live",
            oneglobal_token="tok",
            oneglobal_test_iccid="",  # vacío: el validador de modo live lo exige
        )


def test_identificadores_con_formato_invalido_se_rechazan(tmp_path) -> None:
    with pytest.raises(ValidationError, match="ICCID"):
        make_settings(tmp_path, oneglobal_test_iccid="12345")
    with pytest.raises(ValidationError, match="IMEI"):
        make_settings(tmp_path, teltonika_test_imei="abc123")


def test_identificadores_validos(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert settings.test_iccid == TEST_ICCID
    assert settings.device_key_from_imei == TEST_IMEI[:14]  # llave Device de 14 dígitos


def test_base_url_sin_barra_final(tmp_path) -> None:
    settings = make_settings(tmp_path, oneglobal_base_url="https://iot.truphone.com/api/")
    assert settings.oneglobal_base_url == "https://iot.truphone.com/api"


def test_resumen_seguro_nunca_muestra_token(tmp_path) -> None:
    settings = make_settings(tmp_path, oneglobal_token="secreto-largo-123456789")
    summary = str(settings.safe_summary)
    assert "secreto-largo-123456789" not in summary
    assert "<oculto:23>" in summary


def test_carga_unica_desde_env_file(tmp_path) -> None:
    env_path = write_env_file(tmp_path)
    settings = load_settings(env_path)
    try:
        assert settings.test_iccid == TEST_ICCID
        assert get_settings() is settings
    finally:
        import src.config as config_module

        config_module._settings = None


def test_env_vacio_aplica_defaults(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("GLOBAL_API=\nDATABASE_PATH=\n", encoding="utf-8")
    settings = Settings(_env_file=str(env_path))
    assert settings.oneglobal_token == ""
    assert settings.database_path == "./data/sms_tasks.db"  # default, env vacío no pisa
    assert settings.sms_dry_run is True


def test_token_se_lee_de_GLOBAL_API(tmp_path) -> None:
    """El nombre real del proyecto es GLOBAL_API; ONEGLOBAL_TOKEN queda como alias."""
    env_path = tmp_path / ".env"
    env_path.write_text("GLOBAL_API=val GLOBAL\n", encoding="utf-8")
    assert Settings(_env_file=str(env_path)).oneglobal_token == "val GLOBAL"

    # Alias de compatibilidad con el documento de desarrollo original:
    env_alias = tmp_path / "alias.env"
    env_alias.write_text("ONEGLOBAL_TOKEN=val-alias\n", encoding="utf-8")
    assert Settings(_env_file=str(env_alias)).oneglobal_token == "val-alias"

    # Con ambos, el nombre principal (GLOBAL_API) tiene prioridad:
    env_both = tmp_path / "both.env"
    env_both.write_text("GLOBAL_API=principal\nONEGLOBAL_TOKEN=alias\n", encoding="utf-8")
    assert Settings(_env_file=str(env_both)).oneglobal_token == "principal"
