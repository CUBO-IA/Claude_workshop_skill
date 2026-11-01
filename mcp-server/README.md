# Servidor MCP de 1GLOBAL — comandos SMS Teltonika (laboratorio)

Servidor **MCP (Model Context Protocol) en Python** que integra la API de
1GLOBAL (antiguo Truphone, IoT Portal) para transmitir comandos SMS a un
rastreador Teltonika de pruebas, consultar el estado de entrega y recuperar la
**respuesta del equipo** (SMS MO). Diseñado para operar junto a la skill
`teltonika-commands` a través de un agente compatible con MCP (p. ej. OpenClaw).

- **Idioma**: toda la documentación, logs, descripciones de herramientas y
  mensajes están en español.
- **Alcance**: laboratorio de **UN dispositivo de pruebas**. Nada de lotes ni
  flotas.
- **Seguridad primero**: el envío real está deshabilitado por defecto y exige
  un token de aprobación de un solo uso emitido por el operador.
- **Fuente de implementación**: `guide_api_global.md` (integración 1GLOBAL
  verificada en vivo), más el documento de desarrollo adjunto.

---

## 1. Arquitectura

```text
┌───────────────────────────────────────────────────────────────────┐
│ Agente (OpenClaw) + skill teltonika-commands                      │
│  · identifica el comando Teltonika y prepara el SMS EXACTO        │
│  · solicita confirmación explícita al usuario                     │
└──────────────────────────────┬────────────────────────────────────┘
                               │ MCP (stdio)
┌──────────────────────────────▼────────────────────────────────────┐
│ Servidor MCP (este proyecto)                                      │
│  tools: send_sms · get_sms_status · get_sms_response              │
│  security: política de comandos + approval token (HMAC 1 uso)     │
│  services: SmsService · SmsWorker (gracia, sondeo, respuesta MO)  │
│  providers: 1GLOBAL live (HTTPX) | replay (fixtures JSON)         │
│  persistencia: SQLite (tareas + auditoría + tokens usados)        │
└──────────────────────────────┬────────────────────────────────────┘
                               │ HTTPS (solo en modo live)
                     https://iot.truphone.com/api  (rutas SMS: v2.0)
```

Reglas de reparto (documentadas también en la skill):

- La **skill** es la capa de conocimiento: decide cuál comando Teltonika se
  necesita y forma el texto SMS (incluyendo los dos espacios iniciales de
  autenticación si no hay credenciales SMS configuradas).
- El **MCP** es la frontera de seguridad y comunicación: valida destino,
  comando y autorización; transmite por 1GLOBAL; persiste y correlaciona la
  respuesta del equipo. **No importa ni ejecuta la skill**: sus herramientas
  son invocables por cualquier cliente MCP autorizado.

### Ids locales vs ids externos

| Identificador | Ejemplo | Quién lo crea | Uso |
| --- | --- | --- | --- |
| `local_task_id` | `smsq_<huella>_<único>` | Este servidor | Llave local para `get_sms_status`/`get_sms_response`, idempotencia, auditoría |

**Semántica del `local_task_id`** (como en el proyecto previo): el identificador **remite al dispositivo**; la huella son los 8 primeros hex del SHA-256 del ICCID de destino (`smsq_9e7f…_a1b2c3d4`), de modo que se pueda atribuir cada tarea a su equipo sin exponer el ICCID completo. Además, en este laboratorio de UN dispositivo las consultas no necesitan el id: `get_sms_status` / `get_sms_response` sin `local_task_id` usan `target="test"` y devuelven la tarea **MÁS RECIENTE** del equipo de pruebas configurado en el `.env` del servidor.
| `external_task_id` | `8f3c1d2e-…` | 1GLOBAL (extraído del `resourceURL`) | SOLO para sondear el estado en 1GLOBAL |

Nunca se sondea a 1GLOBAL con el id local (devolvería error) ni se presentan
como equivalentes.

### Entrega ≠ ejecución del comando

- `sent`: 1GLOBAL aceptó el SMS (aún no es entrega).
- `delivered`: 1GLOBAL confirmó la entrega en el equipo.
- **La respuesta del dispositivo** (contenido de un SMS MO detectado y
  correlacionado) es la única señal de que el equipo reaccionó; aun así se
  reporta de forma conservadora, sin afirmar "ejecución exitosa" más allá del
  contenido recibido.

---

## 2. Requisitos e instalación (Ubuntu)

```bash
# Python 3.12+ (probado con 3.14)
python3 --version

# Crear el entorno virtual y las dependencias
cd mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install mcp httpx pydantic pydantic-settings aiosqlite
# Dependencias de desarrollo (pruebas, lint, tipos):
pip install pytest pytest-asyncio respx ruff mypy
```

> Alternativa con `uv`: `uv venv && uv pip install -r <deps> …` (mismos paquetes).

## 3. Configuración del entorno

```bash
cp .env.example .env          # .env está excluido de Git
nano .env
```

Variables principales (validadas con Pydantic al arrancar):

| Variable | Valor por defecto | Notas |
| --- | --- | --- |
| `ONEGLOBAL_BASE_URL` | `https://iot.truphone.com/api` | Las rutas SMS usan `v2.0` |
| `GLOBAL_API` (alias `ONEGLOBAL_TOKEN`) | *(vacío)* | **Solo necesario en modo live**; nunca se loguea completo |
| `ONEGLOBAL_AUTH_SCHEME` | `Token` | Esquema real del portal (no se sustituye por Bearer) |
| `ONEGLOBAL_MODE` | `replay` | `replay` = sin red ni token; `live` = HTTPS real |
| `TELTONIKA_TEST_IMEI` | *(vacío)* | 14–15 dígitos; la llave Device = IMEI sin el último dígito |
| `ONEGLOBAL_TEST_ICCID` | *(vacío)* | 19–20 dígitos que empiezan por `89`; **único destino autorizado** |
| `MCP_SERVER_NAME` / `MCP_TRANSPORT` | `oneglobal-sms` / `stdio` | |
| `SMS_DRY_RUN` | `true` | **Nunca transmite** hasta que se cambie |
| `SMS_LIVE_ENABLED` | `false` | Puerta 1 de la transmisión real |
| `SMS_REQUIRE_CONFIRMATION` | `true` | Puerta 2: exige `approval_token` de un solo uso |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | `10` / `3` | Reintentos SOLO en GET seguros (ver §8) |
| `ONEGLOBAL_RATE_LIMIT_PER_MINUTE` | `100` | Depósito de tokens, sin ráfagas |
| `SMS_POLL_INTERVAL_SECONDS` | `5` | Cadencia del worker |
| `SMS_SENT_GRACE_SECONDS` | `600` | Un SMS joven no muere aunque se agoten los reintentos |
| `SMS_RESPONSE_WINDOW_SECONDS` | `3600` | Ventana para buscar la respuesta MO |
| `SMS_POLL_MAX_AGE_SECONDS` | `86400` | Backlog mayor ya no se sondea |
| `SMS_MAX_RETRIES` | `3` | Reintentos de sondeo |
| `SMS_DEVICE_MAX_PER_HOUR` | `5` | Anti-bucles del LLM (config extra) |
| `DATABASE_PATH` | `./data/sms_tasks.db` | SQLite (tareas + auditoría) |
| `LOG_LEVEL` | `INFO` | Logs a **stderr** (stdout es del protocolo MCP) |

Validaciones implementadas: en `replay` el token NO es necesario; en `live`
el arranque falla sin token y sin ICCID de pruebas. Los formatos de ICCID e
IMEI se validan (longitud/dígitos), lo cual **no** demuestra que existan en
1GLOBAL. Los identificadores jamás se escriben en el código: solo se leen del
`.env`, y nunca se muestran completos por las herramientas.

## 4. Modos de operación

| Modo | Qué hace | Red |
| --- | --- | --- |
| **Dry-run** (`dry_run=true` o `SMS_DRY_RUN=true`) | Prepara y valida el SMS; tarea `pending` que nunca se transmite | No |
| **Replay** (`ONEGLOBAL_MODE=replay`, `dry_run=false`) | Ciclo COMPLETO simulado con fixtures reales capturados (envío → sent → delivered → respuesta); ideal para desarrollar y para demos | No |
| **Live** (`live` + puertas habilitadas + token de aprobación) | Transmisión real al ICCID de pruebas | Sí |

En modo replay, escenarios adicionales eligiendo el prefijo de `request_id`:

| Prefijo | Escenario |
| --- | --- |
| *(ninguno)* | Ciclo completo exitoso con respuesta del equipo |
| `noresp-` | Ciclo completo sin respuesta del equipo |
| `ambiguous-` | Dos candidatas de respuesta no correlacionables (se declara `ambiguous`) |
| `fail-` | La API falla (HTTP 503, fixture `sms_failure.json`) |
| `malformed-` | Respuesta de envío sin id usable → tarea "sumisión incierta" |

## 5. Seguridad de la transmisión real (puertas y aprobación)

La transmisión real exige **todas** estas condiciones en el servidor:

1. `ONEGLOBAL_MODE=live` (con `GLOBAL_API` e ICCID válidos).
2. `SMS_LIVE_ENABLED=true`.
3. Destino `test` resuelto al ICCID autorizado (un agente no puede cambiarlo).
4. Comando en la **allowlist** de riesgo bajo (`web_connect`, `getinfo`,
   `getver`, `getstatus`, `getgps`, `ggps`, `getio`, `readio`, `getparam`,
   `getrecord`, `countrecs`). Están **bloqueados** `setparam`, `cpureset`,
   `deleterecords`, `flush`, `setdigout` (y variantes de reset).
5. Si `SMS_REQUIRE_CONFIRMATION=true`: un **token de aprobación de un solo
   uso**, emitido por el OPERADOR fuera del LLM. El token (HMAC con secreto
   local 0600 en `data/approval_secret.key`) enlaza ICCID + hash de la carga
   exacta + expiración (15 min por defecto) y se consume al primer uso
   (SQLite). Un `"confirmed": true` generado por el modelo NO es autorización.

Pasos exactos para habilitar UNA transmisión real segura:

```bash
cd mcp-server
# 1. Operador: configura el modo live y desbloquea el envío en .env
#    ONEGLOBAL_MODE=live, GLOBAL_API=<tu token>,
#    TELTONIKA_TEST_IMEI=<IMEI>, ONEGLOBAL_TEST_ICCID=<ICCID>,
#    SMS_DRY_RUN=false, SMS_LIVE_ENABLED=true
# 2. Operador: emite el token para la carga EXACTA (15 min de vigencia)
.venv/bin/python main.py mint "  web_connect" --ttl 900
#    → copia el token impreso (es secreto de un solo uso)
# 3. Agente (tras confirmación explícita del usuario) invoca:
#    send_sms(target="test", text="  web_connect", dry_run=false,
#             approval_token=<token impreso>)
# 4. Verifica con get_sms_status y get_sms_response.
# 5. Operador: vuelve a cerrar las puertas (SMS_LIVE_ENABLED=false).
```

El token no debe pegarse en logs, tickets ni chat: es secreto de un solo uso.

## 6. Arranque del servidor MCP

```bash
cd mcp-server
source .venv/bin/activate
python main.py serve --env-file .env      # stdio (por defecto)
```

El servidor **no produce salida en stdout** aparte del protocolo MCP; los
logs van a stderr. El worker de sondeo arranca con el lifespan del servidor;
además existe un proceso persistente alternativo:

```bash
python main.py worker --env-file .env     # solo el worker (uso avanzado)
```

Si el cliente MCP desconecta, el proceso muere y el worker con él: **eso está
cubierto** porque `get_sms_status` y `get_sms_response` refrescan/detectan
bajo demanda. Las tareas `pending` de transmisión real dejadas por un corte se
marcan `uncertain` al reiniciar y **nunca se reenvían a ciegas** (conciliar
manualmente con el listado de detalles de SMS).

## 7. Integración con un cliente MCP (OpenClaw)

Registra el servidor en la configuración del cliente MCP compatible, por
ejemplo estilo Claude/OpenClaw:

```json
{
  "mcpServers": {
    "oneglobal-sms": {
      "command": "/ruta/a/mcp-server/.venv/bin/python",
      "args": ["main.py", "serve", "--env-file", ".env"],
      "cwd": "/ruta/a/mcp-server"
    }
  }
}
```

Flujo esperado con la skill `teltonika-commands`:

1. **Consulta** — «¿Cuál es el comando para FOTA WEB?» → la skill responde
   `web_connect` con su fuente. **No se invoca ninguna herramienta MCP.**
2. **Preparación** — «Prepara el SMS» → la skill forma `text` exacto
   (`"  web_connect"` con DOS espacios si no hay credenciales SMS) y muestra
   resumen (destino enmascarado, riesgos, «preparado, no enviado»).
3. **Ejecución** — «Envíalo al equipo de pruebas» → la skill solicita
   confirmación explícita del usuario; con `SMS_DRY_RUN=true` el servidor solo
   prepara; una transmisión real exige las puertas del §5 y el
   `approval_token` del operador. El MCP devuelve `local_task_id`.
4. **Seguimiento** — «¿Se entregó?» → `get_sms_status(local_task_id)` o, si el agente no recuerda el id, `get_sms_status()` sin id: el servidor resuelve la última tarea del dispositivo de pruebas.
5. **Respuesta** — «¿Qué respondió el equipo?» →
   `get_sms_response(local_task_id)` o `get_sms_response()` sin id (misma resolución por dispositivo).

La skill resuelve `target="test"` internamente: **nunca** pregunta IMEI/ICCID
ni los obtiene; el MCP nunca devuelve los identificadores completos (van
enmascarados, p. ej. `894447…750`). Si una herramienta MCP no está disponible,
el agente debe decir que no puede enviar nada: no hay rutas alternativas.

## 8. Fiabilidad HTTP (según guía y documento)

- Timeout por intento: 10 s; **3** reintentos SOLO para GET seguros ante
  `408/425/429/500/502/503/504` o fallo de red; los 4xx no reintentan.
- Backoff exponencial 300 ms ×2^n con techo 4 s y jitter aleatorio.
- **POST de envío: nunca se reintenta automáticamente.** Un timeout pudo ser
  aceptado por 1GLOBAL: la tarea queda `uncertain` y se concilia manualmente.
  Los reintentos automáticos de POST solo se habilitarían si se verificaran
  semánticas de idempotencia del proveedor.
- Rate limit: depósito de tokens, 100 peticiones/min por defecto.
- Logs con redacción de `Authorization` y del token.

## 9. Pruebas

```bash
cd mcp-server
source .venv/bin/activate
pytest -q                  # 74 pruebas: unitarias + integración MCP
ruff check src tests main.py
mypy src
```

Cobertura de los 22 casos exigidos: validación de configuración, resolución
de `target="test"`, rechazo de destinos no autorizados, preservación de
espacios iniciales, cabecera `Authorization: Token`, rutas `v2.0`, extracción
de ids externos (incluida la doble barra), rechazo de respuestas sin id,
normalización de estados, persistencia, idempotencia por `request_id`, sin
reenvío ciego tras timeout, estado por id externo, exclusión de eco,
detección de respuesta válida, rechazo de correlación ambigua, período de
gracia, recuperación tras reinicio, dry-run sin proveedor, replay sin
credenciales, transmisión no autorizada rechazada y descubrimiento/invocación
MCP con el SDK oficial. Las pruebas NO hacen llamadas de red reales (respx +
replay).

## 10. Ejemplo de dry-run con `web_connect`

```json
// send_sms(target="test", command="web_connect", dry_run=true)
{
  "success": true,
  "mode": "dry_run",
  "local_task_id": "smsq_d25b0bb22b64",
  "external_task_id": null,
  "target": "test",
  "masked_iccid": "894447…750",
  "status": "simulated",
  "message": "SMS preparado y validado. No se realizó ninguna transmisión (dry_run). ..."
}
```

Y el ciclo replay completo (verificado en este repositorio):

```text
send_sms(target="test", text="  web_connect", dry_run=false, request_id="demo-1")
  → status "sent", external_task_id 8f3c1d2e-…, modo replay (SIMULACIÓN)
get_sms_status  → sent  → delivered (delivered_at UTC)
get_sms_response → found=true, response_content="web_connect: conexión con FOTA WEB iniciada, FW 03.27.06"
```

## 11. Solución de problemas

| Síntoma | Causa probable / solución |
| --- | --- |
| `ValidationError: El modo live exige un token` | Estás en `ONEGLOBAL_MODE=live` sin `GLOBAL_API` (o sin ICCID). En laboratorio usa `replay`. |
| `Destino no autorizado` | Solo `target="test"` está permitido; el agente no debe aportar ICCIDs. |
| `no está en la allowlist` / `BLOQUEADO` | El servidor solo permite comandos de riesgo bajo; los de alto riesgo requieren un flujo posterior revisado. |
| `El token de aprobación ya fue consumido` | Cada token sirve UNA vez: pide uno nuevo con `python main.py mint`. |
| `El token de aprobación corresponde a OTRA carga útil` | El token enlaza la carga EXACTA: emite uno para el texto exacto (con sus espacios). |
| `Sumisión incierta` | El POST dudó (timeout/504). NO reenvíes: consulta `get_sms_status`, y si sigue sin id externo, pide conciliación manual al operador. |
| Logs no aparecen | Van a **stderr**, no stdout: revisa el canal de errores del cliente MCP. |
| La herramienta no responde a `reference_id` | Verifica que usas el `local_task_id` (`smsq_…`) devuelto por `send_sms`. |

## 12. Precauciones y limitaciones declaradas

- **Laboratorio de un dispositivo**: no lotes, no flota, no segundos destinos.
- La asociación IMEI↔ICCID del `.env` NO se verifica contra 1GLOBAL: solo el
  ICCID autorizado se usa como destino real; el IMEI es referencia local.
- La correlación de respuesta MO es heurística (eco excluido + ventana +
  un solo comando activo por ICCID). Ante ambigüedad se declara `ambiguous`.
- No se inventan endpoints: solo los documentados en la guía (`send_sms`,
  `send_sms/status/{id}`, `send_sms/status/` con filtros). El CDR
  (`GET /v2.0/sims/{iccid}/cdrs/`) se deja documentado como alternativa
  histórica, no implementado.
- No se almacenan credenciales en SQLite; el secreto de aprobación vive solo
  en `data/` (excluido de Git junto con `data/*.db`).
- `data/sms_tasks.db` y `data/approval_secret.key` son locales del
  laboratorio: no los compartas ni los versiones.

## 13. Estructura del proyecto

```text
mcp-server/
├── src/
│   ├── index.py                  # ensamblado MCPServer + lifespan + CLI helpers
│   ├── config.py                 # Settings Pydantic (carga única, validación por modo)
│   ├── models.py                 # entradas/salidas MCP (español)
│   ├── tools/                    # send_sms · get_sms_status · get_sms_response
│   ├── providers/oneglobal_client.py  # cliente live (HTTPX) + replay + parsers
│   ├── security/                 # command_policy.py · approval.py
│   ├── services/                 # sms_service · sms_repository (SQLite) · sms_worker
│   └── utils/                    # retry · rate_limiter · logging_utils · time_utils
├── tests/
│   ├── fixtures/                 # JSON de replay (respuestas reales capturadas)
│   ├── unit/                     # config · policy · client · parsers · service
│   └── integration/              # herramientas MCP vía SDK · ciclo de vida con worker
├── data/                         # SQLite + secreto de aprobación (NO versionado)
├── main.py                       # entrypoint: serve | worker | mint
├── .env.example                  # plantilla versionada
├── .env                          # local con secretos (NUNCA versionado)
├── pyproject.toml · .gitignore · README.md
```
