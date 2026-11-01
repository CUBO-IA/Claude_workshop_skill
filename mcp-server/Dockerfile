# Servidor MCP 1GLOBAL — imagen del laboratorio
# Sin secretos dentro de la imagen: la configuración se inyecta con
# --env-file / compose (env_file) o variables de entorno al ejecutar.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencias de ejecución (mismas de pyproject.toml, sin build extras)
RUN pip install \
    "mcp>=2.3,<3" \
    "httpx>=0.27" \
    "pydantic>=2.7" \
    "pydantic-settings>=2.2" \
    "aiosqlite>=0.20" \
    "uvicorn>=0.30"

# Código, punto de entrada y fixtures de replay (necesarias para ONEGLOBAL_MODE=replay)
COPY src/ src/
COPY main.py pyproject.toml ./
COPY tests/fixtures tests/fixtures

# Directorio de datos local (SQLite + secreto de aprobación): montable como volumen
RUN mkdir -p /app/data \
    && useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# Uso: docker run -i --rm <imagen> [--env-file /app/.env] serve
#      docker run ... worker | mint "<payload>"
ENTRYPOINT ["python", "main.py"]
# Por defecto: MCP por stdio (para invocación desde un cliente MCP/skill)
CMD ["serve"]
