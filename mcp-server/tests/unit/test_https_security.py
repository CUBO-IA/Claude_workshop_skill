"""HTTPS obligatorio para el transporte por red y generación de certificados.

El stdio local no es transporte de red: no aplica TLS. Cualquier arranque del
MCP por HTTP/SSE sin certificado SE NIEGA a arrancar (mensaje en español).
La verificación end-to-end de HTTPS con clientes MCP se ejecuta en la demo en
vivo (Docker/contenedor); aquí se coteja la frontera de arranque.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.index import gen_certs, require_tls_for_http
from tests.conftest import make_settings


def test_http_sin_certificado_se_niega(tmp_path) -> None:
    settings = make_settings(
        tmp_path, mcp_transport="streamable-http", mcp_tls_certfile="", mcp_tls_keyfile=""
    )
    with pytest.raises(RuntimeError, match="exige HTTPS"):
        require_tls_for_http(settings)


def test_http_con_certificado_inexistente_se_niega(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_transport="streamable-http",
        mcp_tls_certfile=str(tmp_path / "inexistente.crt"),
        mcp_tls_keyfile=str(tmp_path / "inexistente.key"),
    )
    with pytest.raises(RuntimeError, match="exige HTTPS"):
        require_tls_for_http(settings)


def test_http_con_cert_vacio_y_clave_presente_se_niega(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        mcp_transport="sse",
        mcp_tls_certfile="",
        mcp_tls_keyfile=str(tmp_path / "solo-clave.key"),
    )
    with pytest.raises(RuntimeError, match="exige HTTPS"):
        require_tls_for_http(settings)


def test_http_con_certificados_validos_permite_arrancar(tmp_path) -> None:
    key = tmp_path / "k.key"
    key.write_bytes(b"clave-simulada-para-configuracion")
    cert = tmp_path / "c.crt"
    cert.write_bytes(b"cert-simulado-para-configuracion")
    settings = make_settings(
        tmp_path,
        mcp_transport="streamable-http",
        mcp_tls_certfile=str(cert),
        mcp_tls_keyfile=str(key),
    )
    certfile, keyfile = require_tls_for_http(settings)
    assert Path(certfile) == cert and Path(keyfile) == key


def test_stdio_no_aplica_tls(tmp_path) -> None:
    settings = make_settings(tmp_path)  # stdio, sin TLS configurado
    assert settings.mcp_transport == "stdio"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl no disponible")
def test_gen_certs_genera_par_valido(tmp_path) -> None:
    cert_path, key_path = gen_certs(tmp_path / "certs", days=30)
    assert cert_path.is_file() and key_path.is_file()
    san = subprocess.run(
        ["openssl", "x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "localhost" in san and "127.0.0.1" in san
    assert os.stat(key_path).st_mode & 0o777 == 0o600  # clave con permisos 0600
