"""Caso 12 del documento: política de seguridad de comandos."""

from __future__ import annotations

import pytest

from src.security.approval import ApprovalError, ApprovalManager
from src.security.command_policy import (
    PolicyViolation,
    assert_target_allowed,
    extract_command_name,
    validate_command_name,
    validate_payload,
)


def test_destino_test_se_resuelve() -> None:
    iccid = assert_target_allowed("test", "8944474400001005750")
    assert iccid == "8944474400001005750"


def test_destino_desconocido_se_rechaza() -> None:
    with pytest.raises(PolicyViolation, match="no autorizado"):
        assert_target_allowed("8944474400009999999", "8944474400001005750")


def test_destino_test_sin_configurar_se_rechaza() -> None:
    with pytest.raises(PolicyViolation, match="ONEGLOBAL_TEST_ICCID"):
        assert_target_allowed("test", "")


def test_espacios_iniciales_preservados() -> None:
    # La validación NO recorta ni altera el SMS preparado por la skill.
    validate_payload("  web_connect")
    assert extract_command_name("  web_connect") == "web_connect"


def test_sms_largo_se_rechaza() -> None:
    payload = "  getparam " + ";".join(str(n) for n in range(1, 200))
    with pytest.raises(PolicyViolation, match="160"):
        validate_payload(payload)


def test_caracteres_de_control_se_rechazan() -> None:
    with pytest.raises(PolicyViolation, match="inyección"):
        validate_payload("  web_connect\nrm -rf /")


def test_marcadores_sin_resolver_se_rechazan() -> None:
    with pytest.raises(PolicyViolation, match="marcadores"):
        validate_payload("  readio <avl_id>")


def test_comando_bloqueado() -> None:
    for name in ("setparam", "cpureset", "deleterecords", "flush", "setdigout"):
        with pytest.raises(PolicyViolation, match="BLOQUEADO"):
            validate_command_name(name)


def test_comando_desconocido_rechazado() -> None:
    with pytest.raises(PolicyViolation, match="allowlist"):
        validate_command_name("format_factory")


def test_comando_sensible_a_mayusculas() -> None:
    with pytest.raises(PolicyViolation, match="sensibles"):
        validate_command_name("WEB_CONNECT")


def test_allowlist_acepta_riesgo_bajo() -> None:
    info = validate_command_name("getinfo")
    assert info.effect == "read_only"
    assert info.risk == "Bajo"


# --------------------------------------------------------------------------- #
# Token de aprobación de un solo uso
# --------------------------------------------------------------------------- #
def test_token_enlaza_iccid_carga_y_vigencia(tmp_path) -> None:
    manager = ApprovalManager(secret=b"a" * 64, ttl_seconds=60)
    token = manager.mint(iccid="8944474400001005750", payload="  web_connect")

    assert manager.verify(token, iccid="8944474400001005750", payload="  web_connect") is not None
    with pytest.raises(ApprovalError, match="otro dispositivo"):
        manager.verify(token, iccid="8944474400009999999", payload="  web_connect")
    with pytest.raises(ApprovalError, match="OTRA carga"):
        manager.verify(token, iccid="8944474400001005750", payload="  getinfo")
    with pytest.raises(ApprovalError, match="expir"):
        manager.verify(token, iccid="8944474400001005750", payload="  web_connect", now=10**12)


def test_token_falso_rechazado() -> None:
    manager = ApprovalManager(secret=b"a" * 64)
    with pytest.raises(ApprovalError):
        manager.verify("falso.token", iccid="8944474400001005750", payload="  web_connect")
    with pytest.raises(ApprovalError, match="un solo uso"):
        manager.verify(None, iccid="8944474400001005750", payload="  web_connect")
