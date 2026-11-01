# Claude Workshop — Skill + MCP + Agente (Teltonika / 1GLOBAL)

Laboratorio que integra **conocimiento de comandos Teltonika** (skill de agente)
con un **servidor MCP** capaz de transmitir esos comandos como SMS por la
**API de 1GLOBAL (antiguo Truphone, IoT Portal)** a un equipo de pruebas, y de
seguir su entrega y la **respuesta del dispositivo**.

La regla de reparto es simple:

- La **skill** es la capa de conocimiento y orquestación: sabe cuál comando
  Teltonika se necesita, valida sintaxis/compatibilidad y prepara el texto
  SMS EXACTO (incluyendo los dos espacios iniciales de autenticación).
- El **servidor MCP** es la frontera de seguridad y comunicación: resuelve el
  dispositivo de pruebas, aplica la política de comandos, cursa el SMS por
  1GLOBAL, persiste la tarea y correlaciona la respuesta del equipo.

## Contenido del repositorio

| Carpeta / archivo | Qué es |
| --- | --- |
| `skill/teltonika-commands/` | Skill **teltonika-commands**: catálogo de comandos SMS/GPRS Teltonika (familia FMB/FMC), preparación local del SMS sin red y contrato de integración MCP. Ver su [SKILL.md](skill/teltonika-commands/SKILL.md) |
| `mcp-server/` | Servidor MCP **oneglobal-sms** (Python): herramientas `send_sms`, `get_sms_status` y `get_sms_response` sobre el SDK oficial de MCP, con transporte live (HTTPX) y modo replay. Documentación completa en [mcp-server/README.md](mcp-server/README.md) |
| `.sources/guide_api_global.md` | Guía técnica de la integración 1GLOBAL (referencia interna, **excluida de Git**) |
| `.env` / `mcp-server/.env` | Configuración local con secretos (**excluidos de Git**); las plantillas versionables son `.env.example` y `mcp-server/.env.example` |

## La skill: `teltonika-commands`

Consultar, preparar y **envío autorizado** de comandos Teltonika:

- **Consulta** — «¿Cuál es el comando para conectar con FOTA WEB?» → responde
  `web_connect` con su fuente (Teltonika Wiki). **No envía SMS.**
- **Preparación** — valida modelo/familia, modo de autenticación SMS y clasifica
  el riesgo usando el catálogo (`references/commands.json`, 16 comandos
  seleccionados) y `references/compatibility.json`.
- **Ejecución** — «Envía `web_connect` al FMC130 de pruebas» → usa
  `target: "test"` (el servidor MCP resuelve el ICCID oculto), pide
  confirmación explícita al usuario y envía **solo** vía MCP.

Recursos: `references/sources.md` (fuentes y límites) y
`scripts/search-commands.py` (búsqueda/formato local, sin red).

## El servidor MCP: `oneglobal-sms`

```text
Enviados a 1GLOBAL (rutas SMS v2.0)     Consultados por el agente (MCP/stdio)
send_sms → POST send_sms/ + id externo  get_sms_status / get_sms_response
.estado → GET send_sms/status/{ext}/      (con o sin id: última tarea del
detalles → GET send_sms/status/?…          dispositivo de pruebas)
```

- **Tres herramientas MCP** con esquemas y descripciones en español.
- **Modos**: `dry_run` (preparar sin transmitir, por defecto), `replay`
  (ciclo completo simulado con fixtures reales, sin red ni token) y `live`
  (transmisión real al único ICCID autorizado).
- **Persistencia SQLite**: tareas con id local que **remite al dispositivo**
  (`smsq_<huella-del-ICCID>_<único>`), idempotencia por `request_id`,
  auditoría y tokens de aprobación usados.
- **Fiabilidad**: reintentos GET solo ante 408/425/429/5xx, ningún reenvío
  ciego de POST dudosos (tareas "inciertas" + conciliación manual),
  rate limit 100/min, período de gracia de entrega y correlación
  conservadora de la respuesta MO (eco excluido, ambigüedad declarada).

### Seguridad

Las transmisiones reales exigen **todas** estas puertas en el servidor (un
booleano generado por el LLM NO es autorización):

1. `ONEGLOBAL_MODE=live` con token (`GLOBAL_API` en el `.env` del servidor).
2. `SMS_LIVE_ENABLED=true` (deshabilitado por defecto).
3. Destino único `target="test"` resuelto en el servidor (ICCID/IMEI jamás se
   exponen completos: quedan como `<oculto:N>` / `894447…750`).
4. Comando en la **allowlist** de riesgo bajo; bloqueados
   `setparam`, `cpureset`, `deleterecords`, `flush`, `setdigout`.
5. **Token de aprobación de un solo uso** emitido por el operador fuera del
   modelo: `python main.py mint "  web_connect" --ttl 900` (HMAC sobre
   ICCID + hash de la carga exacta + vigencia; secreto local 0600).

## Cómo empezar

```bash
# 1) Configurar el servidor MCP (replay funciona sin credenciales)
cd mcp-server
cp .env.example .env          # completa TELTONIKA_TEST_IMEI / ONEGLOBAL_TEST_ICCID
python3 -m venv .venv && source .venv/bin/activate
pip install mcp httpx pydantic pydantic-settings aiosqlite

# 2) Arrancar el servidor MCP (stdio) para un cliente compatible
python main.py serve --env-file .env

# 3) Pruebas y verificación
pip install pytest pytest-asyncio respx ruff mypy
pytest -q && ruff check src tests main.py && mypy src
```

Detalles de instalación, integración con OpenClaw, solución de problemas y
explicación detallada en `mcp-server/README.md`.

## Estado actual del laboratorio

- ✅ Servidor MCP completo y verificado (79 pruebas: unitarias + integración
  MCP por el protocolo oficial, sin llamadas de red reales).
- ✅ Ciclo replay completo: dry-run → envío → `delivered` → respuesta del equipo.
- ⏳ Transmisión **real**: pendiente de config y habilitación por el operador
  (ver "Seguridad"): aún `SMS_DRY_RUN=true` y `SMS_LIVE_ENABLED=false`.

## Licencia

Véase [LICENSE](LICENSE).
