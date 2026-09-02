"""Выгрузка итоговых анкет в Google Sheets.

Подключение ленивое: клиент создаётся при первой записи, чтобы отсутствие
интернета или ключа на старте не мешало боту работать. Любая ошибка
логируется и НЕ прерывает диалог с пользователем.

Как включить:
  1. Google Cloud Console -> создать сервисный аккаунт -> скачать JSON-ключ;
  2. включить Google Sheets API и Google Drive API;
  3. открыть доступ к таблице на e-mail сервисного аккаунта (Редактор);
  4. в config.yaml -> google_sheets.enabled: true и указать spreadsheet_id.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import SheetsConfig, Step

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class SheetsExporter:
    """Дописывает по строке на каждого успешно прошедшего опрос кандидата."""

    def __init__(self, config: SheetsConfig, steps: Sequence[Step]) -> None:
        self.config = config
        self.steps = [step for step in steps if step.in_summary]
        self._worksheet: Any = None
        self._lock = threading.Lock()
        self._failed = False

    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def header(self) -> list[str]:
        """Строка заголовков — формируется из подписей шагов конфига."""
        return (
            ["Дата и время"]
            + [step.label or step.key for step in self.steps]
            + ["Телеграм", "User ID", "Статус"]
        )

    def row(
        self,
        *,
        answers: Mapping[str, Any],
        username: str,
        user_id: int,
        status: str,
        created_at: datetime,
    ) -> list[str]:
        """Готовит строку в том же порядке, что и header()."""
        values = [created_at.strftime("%d.%m.%Y %H:%M:%S")]
        for step in self.steps:
            values.append(str(answers.get(step.key, "")))
        values += [username, str(user_id), status]
        return values

    # ------------------------------------------------------------------ #

    def _get_worksheet(self) -> Any:
        """Ленивая авторизация и получение листа (выполняется в отдельном потоке)."""
        if self._worksheet is not None:
            return self._worksheet

        with self._lock:
            if self._worksheet is not None:
                return self._worksheet

            import gspread  # локальный импорт: без Sheets зависимость не нужна
            from google.oauth2.service_account import Credentials

            key_path = Path(self.config.credentials_file)
            if not key_path.exists():
                raise FileNotFoundError(
                    f"Файл ключа сервисного аккаунта не найден: {key_path.resolve()}"
                )

            creds = Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key(self.config.spreadsheet_id)

            try:
                worksheet = spreadsheet.worksheet(self.config.worksheet)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=self.config.worksheet, rows=1000, cols=max(10, len(self.header()) + 2)
                )
                logger.info("В таблице создан лист '%s'", self.config.worksheet)

            if self.config.write_header and not worksheet.get_all_values():
                worksheet.append_row(self.header(), value_input_option="USER_ENTERED")
                logger.info("В лист '%s' записана строка заголовков", self.config.worksheet)

            self._worksheet = worksheet
            logger.info(
                "Google Sheets подключён: таблица %s, лист '%s'",
                self.config.spreadsheet_id,
                self.config.worksheet,
            )
            return worksheet

    def _append_sync(self, row: Sequence[str]) -> None:
        worksheet = self._get_worksheet()
        worksheet.append_row(list(row), value_input_option="USER_ENTERED")

    # ------------------------------------------------------------------ #

    async def append(
        self,
        *,
        answers: Mapping[str, Any],
        username: str,
        user_id: int,
        status: str = "Прошёл отбор",
        created_at: datetime | None = None,
    ) -> bool:
        """Добавляет строку в таблицу. Возвращает True при успехе."""
        if not self.enabled:
            return False

        row = self.row(
            answers=answers,
            username=username,
            user_id=user_id,
            status=status,
            created_at=created_at or datetime.now(),
        )
        try:
            await asyncio.to_thread(self._append_sync, row)
            logger.info("Анкета кандидата %s выгружена в Google Sheets", user_id)
            self._failed = False
            return True
        except Exception as exc:  # noqa: BLE001 — Sheets не должен ронять диалог
            # Сбрасываем клиент: возможно, протух токен — на следующей записи переподключимся.
            self._worksheet = None
            if not self._failed:
                logger.error(
                    "Не удалось выгрузить анкету %s в Google Sheets: %s", user_id, exc,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                self._failed = True
            else:
                logger.error("Google Sheets снова недоступен (%s): %s", user_id, exc)
            return False

    async def check_connection(self) -> bool:
        """Проверка доступа к таблице при старте бота (не критичная)."""
        if not self.enabled:
            logger.info("Выгрузка в Google Sheets отключена в конфиге")
            return False
        try:
            await asyncio.to_thread(self._get_worksheet)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Google Sheets недоступен на старте (бот продолжит работу): %s", exc
            )
            return False
