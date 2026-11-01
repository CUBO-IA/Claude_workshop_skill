"""Ensamblado e inicialización del servidor MCP de 1GLOBAL.

Crea la instancia de ``MCPServer`` (SDK oficial `mcp` 2.x), registra las tres
herramientas y configura el lifespan que abre/cierra SQLite y arranca el
worker en background (sin que las herramientas dependan de él).

Transporte por defecto: stdio. Para un cliente compatible con MCP (p. ej. la
skill `teltonika-commands` a través de OpenClaw), registra este servidor en la
configuración del cliente MCP con el comando:

    python main.py serve --env-file .env
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from src.config import Settings, load_settings
from src.providers.oneglobal_client import OneGlobalClient
from src.security.approval import ApprovalManager, load_or_create_secret
from src.services.sms_repository import SmsRepository
from src.services.sms_service import SmsService
from src.services.sms_worker import SmsWorker
from src.tools.get_sms_response import register as register_response
from src.tools.get_sms_status import register as register_status
from src.tools.send_sms import register as register_send
from src.utils.logging_utils import configure_logging, get_logger

logger = get_logger("index")

SERVER_INSTRUCTIONS = (
    "Servidor MCP de 1GLOBAL (laboratorio de un solo dispositivo Teltonika). Reglas: el único "
    "destino autorizado es el lógico 'test', resuelto internamente con ONEGLOBAL_TEST_ICCID; "
    "nunca preguntes por IMEI/ICCID ni intentes otros destinos. Por defecto el servidor no "
    "transmite (SMS_DRY_RUN=true): usa dry_run=true para preparar. Una transmisión real exige "
    "SMS_LIVE_ENABLED y un token de aprobación de un solo uso emitido por el operador con "
    "'python main.py mint' (nunca lo generes ni lo inventes). Tras un timeout NO repitas el "
    "envío: consulta get_sms_status y get_sms_response con el local_task_id devuelto. La "
    "aceptación de la API NO equivale a entrega ni a ejecución del comando en el equipo."
)


@dataclass
class Components:
    """Componentes del servidor construidos una sola vez (inicialización única)."""

    settings: Settings
    repo: SmsRepository
    client: OneGlobalClient
    service: SmsService
    worker: SmsWorker


def create_components(settings: Settings) -> Components:
    """Construye servicio, cliente, repositorio y worker (sin abrir la BD aún)."""
    repo = SmsRepository(settings.database_path)
    client = OneGlobalClient(settings)
    approval = ApprovalManager(
        load_or_create_secret(settings.approval_secret_path),
        ttl_seconds=settings.approval_ttl_seconds,
    )
    service = SmsService(settings, client, repo, approval)
    worker = SmsWorker(settings, service, repo)
    return Components(settings=settings, repo=repo, client=client, service=service, worker=worker)


def create_app(env_file: str | Path | None = None) -> tuple[MCPServer, Components]:
    """Crea el servidor MCP con sus herramientas y su lifespan."""
    settings = load_settings(env_file)
    # La redacción de secretos se instala en los loggers con el token real:
    # aunque algún llamador se olvide, NINGÚN log contiene el token completo.
    configure_logging(settings.log_level, token=settings.oneglobal_token)
    logger.info(
        "Inicializando servidor MCP '%s' (modo=%s, dry_run=%s, live_enabled=%s, token=%s)",
        settings.mcp_server_name,
        settings.oneglobal_mode,
        settings.sms_dry_run,
        settings.sms_live_enabled,
        settings.safe_summary["token"],
    )
    components = create_components(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        await components.repo.connect()
        components.worker.start()
        logger.info("Repositorio abierto en %s y worker arrancado", settings.database_path)
        try:
            yield components
        finally:
            await components.worker.stop()
            await components.client.close()
            await components.repo.close()
            logger.info("Servidor apagado limpiamente")

    mcp = MCPServer(
        name=settings.mcp_server_name,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )
    register_send(mcp, components.service)
    register_status(mcp, components.service)
    register_response(mcp, components.service)
    return mcp, components


def serve(env_file: str | Path | None = None) -> None:
    """Arranca el servidor MCP en el transporte configurado (stdio por defecto)."""
    mcp, _components = create_app(env_file)
    mcp.run(transport="stdio")


async def run_worker_forever(env_file: str | Path | None = None) -> None:
    """Proceso persistente alternativo: solo el worker, sin MCP (uso avanzado)."""
    settings = load_settings(env_file)
    configure_logging(settings.log_level, token=settings.oneglobal_token)
    components = create_components(settings)
    await components.repo.connect()
    try:
        logger.info("Worker persistente arrancado (intervalo=%s s)", settings.sms_poll_interval_seconds)
        await components.worker.run_forever()
    finally:
        await components.client.close()
        await components.repo.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="oneglobal-mcp",
        description="Servidor MCP de 1GLOBAL para comandos SMS Teltonika (laboratorio).",
    )
    parser.add_argument("--env-file", default=".env", help="Archivo .env local (excluido de Git).")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Arranca el servidor MCP por stdio (por defecto).")
    subparsers.add_parser("worker", help="Arranca SOLO el worker de sondeo (proceso persistente).")

    mint = subparsers.add_parser(
        "mint", help="OPERADOR: emite un token de aprobación de un solo uso para una transmisión real."
    )
    mint.add_argument("payload", help="SMS exacto a autorizar, p. ej. '  web_connect' (entre comillas).")
    mint.add_argument("--ttl", type=int, default=None, help="Vigencia en segundos (por defecto 900).")

    args = parser.parse_args(argv)
    env_file: str | Path = args.env_file

    if args.command in (None, "serve"):
        serve(env_file)
    elif args.command == "worker":
        try:
            asyncio.run(run_worker_forever(env_file))
        except KeyboardInterrupt:
            pass
    elif args.command == "mint":
        settings = load_settings(env_file)
        configure_logging(settings.log_level)
        if not settings.test_iccid:
            parser.error("Falta ONEGLOBAL_TEST_ICCID en el .env: el token se enlaza al ICCID de pruebas.")
        approval = ApprovalManager(
            load_or_create_secret(settings.approval_secret_path),
            ttl_seconds=settings.approval_ttl_seconds,
        )
        token = approval.mint(
            iccid=settings.test_iccid, payload=args.payload, ttl_seconds=args.ttl
        )
        print("Token de aprobación de UN solo uso (vigencia %s s):" % (args.ttl or settings.approval_ttl_seconds))
        print(token)
        print(
            "\nSiguiente paso: en la herramienta send_sms usa target='test', text EXACTAMENTE igual "
            "a la carga autorizada, dry_run=false y este approval_token. El token se consume con el primer uso."
        )
