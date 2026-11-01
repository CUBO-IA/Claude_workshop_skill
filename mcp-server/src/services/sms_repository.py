"""Persistencia SQLite de tareas SMS + auditoría + tokens de aprobación usados.

- Fechas en ISO-8601 UTC (con segundos) para comparaciones lexicográficas.
- No se guardan credenciales de la API.
- Inicialización controlada del esquema (migración por ``user_version``).
- Idempotencia: ``request_id`` UNIQUE evita transmisiones duplicadas.
- Los ids local y externo se mantienen en columnas SEPARADAS.
- El claim de tareas evita que dos workers procesen la misma a la vez.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from src.utils.time_utils import iso, utc_now

SCHEMA_VERSION = 1

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS sms_tasks (
        local_task_id     TEXT PRIMARY KEY,
        external_task_id  TEXT,
        request_id        TEXT UNIQUE,
        iccid             TEXT NOT NULL,
        imei              TEXT,
        command           TEXT NOT NULL,
        mode              TEXT NOT NULL,
        status            TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        submitted_at      TEXT,
        delivered_at      TEXT,
        last_checked_at   TEXT,
        response_received INTEGER NOT NULL DEFAULT 0,
        response_content  TEXT,
        response_at       TEXT,
        send_attempts     INTEGER NOT NULL DEFAULT 0,
        poll_attempts     INTEGER NOT NULL DEFAULT 0,
        error_message     TEXT,
        result_details    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT NOT NULL,
        action        TEXT NOT NULL,
        local_task_id TEXT,
        detail        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approval_used (
        jti           TEXT PRIMARY KEY,
        used_at       TEXT NOT NULL,
        local_task_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


class SmsRepository:
    """Acceso asíncrono (aiosqlite) a la base de tareas del laboratorio."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._conn: aiosqlite.Connection | None = None

    # -- ciclo de vida ----------------------------------------------------- #
    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        for statement in _DDL:
            await self._conn.execute(statement)
        await self._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('user_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("El repositorio no está abierto: llama a connect() primero.")
        return self._conn

    # -- utilidades -------------------------------------------------------- #
    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> dict[str, Any]:
        task = dict(row)
        task["response_received"] = bool(task.get("response_received"))
        try:
            task["result_details"] = json.loads(task.get("result_details") or "{}")
        except ValueError:
            task["result_details"] = {}
        return task

    @staticmethod
    def _build_updates(updates: dict[str, Any]) -> tuple[str, list[Any]]:
        columns, params = [], []
        for column, value in updates.items():
            columns.append(f"{column} = ?")
            params.append(_serialize(value))
        return ", ".join(columns), params

    # -- tareas ------------------------------------------------------------ #
    async def create_task(self, record: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Inserta una tarea; si ``request_id`` ya existió devuelve la original.

        Devuelve ``(tarea, creado)``: ``creado=False`` indica solicitud
        duplicada (NO se debe volver a transmitir).
        """
        columns = list(record)
        placeholders = ", ".join("?" for _ in columns)
        try:
            await self.conn.execute(
                f"INSERT INTO sms_tasks ({', '.join(columns)}) VALUES ({placeholders})",
                [_serialize(record[column]) for column in columns],
            )
            await self.conn.commit()
        except aiosqlite.IntegrityError:
            if record.get("request_id"):
                existing = await self.get_by_request_id(record["request_id"])
                if existing is not None:
                    return existing, False
            raise
        return await self.get_task(record["local_task_id"]), True

    async def get_task(self, local_task_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT * FROM sms_tasks WHERE local_task_id = ?", (local_task_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def get_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT * FROM sms_tasks WHERE request_id = ?", (request_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def get_latest_by_iccid(self, iccid: str) -> dict[str, Any] | None:
        """Tarea MÁS RECIENTE de un ICCID (resolución por dispositivo, sin id).

        rowid desempata cuando dos tareas comparten el segundo de created_at.
        """
        cursor = await self.conn.execute(
            "SELECT * FROM sms_tasks WHERE iccid = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (iccid,),
        )
        row = await cursor.fetchone()
        return self._row_to_task(row) if row else None

    async def update_task(self, local_task_id: str, updates: dict[str, Any]) -> bool:
        if not updates:
            return True
        assignments, params = self._build_updates(updates)
        cursor = await self.conn.execute(
            f"UPDATE sms_tasks SET {assignments} WHERE local_task_id = ?",
            [*params, local_task_id],
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def merge_result_details(self, local_task_id: str, extra: dict[str, Any]) -> None:
        task = await self.get_task(local_task_id)
        if task is None:
            return
        merged = {**(task.get("result_details") or {}), **extra}
        await self.update_task(local_task_id, {"result_details": merged})

    # -- consultas del worker / política ------------------------------------ #
    async def tasks_by_status(
        self,
        statuses: list[str],
        *,
        created_from: str | None = None,
        limit: int = 50,
        order: str = "ASC",
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"SELECT * FROM sms_tasks WHERE status IN ({placeholders})"
        params: list[Any] = [*statuses]
        if created_from:
            sql += " AND created_at >= ?"
            params.append(created_from)
        sql += f" ORDER BY created_at {order} LIMIT ?"
        params.append(limit)
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def find_active_by_iccid(
        self, iccid: str, *, exclude_id: str, window_seconds: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Comandos 'activos' del dispositivo: pendientes, en vuelo o entregados
        sin respuesta (regla anti-solape, documento §11)."""
        cutoff = iso(datetime.fromtimestamp(utc_now().timestamp() - window_seconds))
        cursor = await self.conn.execute(
            """
            SELECT * FROM sms_tasks
            WHERE iccid = ? AND local_task_id != ?
              AND created_at >= ?
              AND mode != 'dry_run'
              AND (
                    status IN ('pending', 'transmitting', 'sent', 'uncertain')
                 OR (status = 'delivered' AND response_received = 0)
                  )
            ORDER BY created_at DESC LIMIT ?
            """,
            (iccid, exclude_id, cutoff, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def count_recent_by_device(
        self, iccid: str, *, since_iso: str, exclude_id: str | None = None
    ) -> int:
        """Transmisiones del dispositivo en la última hora (excluyendo la tarea actual)."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM sms_tasks WHERE iccid = ? AND created_at >= ? "
            "AND mode != 'dry_run' AND local_task_id != ?",
            (iccid, since_iso, exclude_id or ""),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # -- auditoría ---------------------------------------------------------- #
    async def audit(self, action: str, local_task_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO audit_events (created_at, action, local_task_id, detail) VALUES (?, ?, ?, ?)",
            (iso(), action, local_task_id, json.dumps(detail or {}, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def list_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        events = []
        for row in rows:
            event = dict(row)
            try:
                event["detail"] = json.loads(event.get("detail") or "{}")
            except ValueError:
                pass
            events.append(event)
        return events

    # -- tokens de aprobación (un solo uso) --------------------------------- #
    async def is_approval_used(self, jti: str) -> bool:
        cursor = await self.conn.execute("SELECT 1 FROM approval_used WHERE jti = ?", (jti,))
        return await cursor.fetchone() is not None

    async def mark_approval_used(self, jti: str, local_task_id: str) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO approval_used (jti, used_at, local_task_id) VALUES (?, ?, ?)",
                (jti, iso(), local_task_id),
            )
            await self.conn.commit()
        except aiosqlite.IntegrityError:
            return False
        return True

    # -- utilidades de fechas ------------------------------------------------ #
    @staticmethod
    def now_minus(seconds: int) -> str:
        return iso(datetime.fromtimestamp(utc_now().timestamp() - seconds))
