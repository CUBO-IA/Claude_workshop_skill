#!/usr/bin/env python3
"""Punto de entrada del servidor MCP de 1GLOBAL.

Subcomandos:
- ``serve``  (por defecto): servidor MCP por stdio.
- ``worker``: worker de sondeo persistente (alternativa segura al lifespan).
- ``mint``   (solo operador): token de aprobación de un solo uso.

Ejemplos (Ubuntu, desde mcp-server/):
    source .venv/bin/activate
    python main.py serve --env-file .env
    python main.py mint "  web_connect" --ttl 900
"""

from __future__ import annotations

from src.index import main

if __name__ == "__main__":
    main()
