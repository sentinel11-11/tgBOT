"""Хранение анкет в SQLite.

Таблица создаётся под текущий сценарий: под каждый шаг опроса заводится
своя колонка. Добавили шаг в конфиг — колонка добавится автоматически
(ALTER TABLE), старые данные не теряются.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import Step

logger = logging.getLogger(__name__)

BASE_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "user_id": "INTEGER NOT NULL",
    "username": "TEXT",
    "first_name": "TEXT",
    "status": "TEXT NOT NULL",
    "answers_json": "TEXT",
    "created_at": "TEXT NOT NULL",
}


class CandidateStorage:
    """Простое синхронное хранилище с асинхронной обёрткой."""

    def __init__(self, path: str, steps: Sequence[Step], enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.steps = list(steps)
        self._ready = False

    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        """Создаёт файл БД, таблицу и недостающие колонки."""
        if not self.enabled:
            logger.info("SQLite отключён в конфиге — анкеты в базу не пишутся")
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            columns = ", ".join(f'"{name}" {ddl}' for name, ddl in BASE_COLUMNS.items())
            with self._connect() as conn:
                conn.execute(f"CREATE TABLE IF NOT EXISTS candidates ({columns})")
                existing = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
                for step in self.steps:
                    if step.key not in existing:
                        conn.execute(f'ALTER TABLE candidates ADD COLUMN "{step.key}" TEXT')
                        logger.info("В таблицу candidates добавлена колонка '%s'", step.key)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_candidates_user_id ON candidates(user_id)"
                )
            self._ready = True
            logger.info("SQLite готов: %s", self.path)
        except sqlite3.Error as exc:
            self._ready = False
            logger.error("Не удалось инициализировать SQLite (%s): %s", self.path, exc)

    # ------------------------------------------------------------------ #

    def _save_sync(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        answers: Mapping[str, Any],
        status: str,
        created_at: datetime,
    ) -> int | None:
        keys = ["user_id", "username", "first_name", "status", "answers_json", "created_at"]
        values: list[Any] = [
            user_id,
            username,
            first_name,
            status,
            json.dumps(dict(answers), ensure_ascii=False),
            created_at.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for step in self.steps:
            if step.key in answers:
                keys.append(step.key)
                values.append(str(answers[step.key]))

        placeholders = ", ".join("?" for _ in keys)
        column_list = ", ".join(f'"{key}"' for key in keys)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO candidates ({column_list}) VALUES ({placeholders})", values
            )
            return cursor.lastrowid

    async def save(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        answers: Mapping[str, Any],
        status: str = "completed",
        created_at: datetime | None = None,
    ) -> int | None:
        """Сохраняет анкету. Ошибки логируются и не прерывают диалог."""
        if not self.enabled or not self._ready:
            return None
        try:
            return await asyncio.to_thread(
                self._save_sync,
                user_id=user_id,
                username=username,
                first_name=first_name,
                answers=answers,
                status=status,
                created_at=created_at or datetime.now(),
            )
        except sqlite3.Error as exc:
            logger.error("Не удалось сохранить анкету в SQLite: %s", exc)
            return None

    # ------------------------------------------------------------------ #

    def _count_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()
            return int(row["c"]) if row else 0

    async def count(self) -> int:
        """Сколько анкет сохранено (для /stats)."""
        if not self.enabled or not self._ready:
            return 0
        try:
            return await asyncio.to_thread(self._count_sync)
        except sqlite3.Error as exc:
            logger.error("Не удалось получить статистику из SQLite: %s", exc)
            return 0
