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
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import SheetsConfig, Step

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Коды, при которых имеет смысл повторить запрос: Google периодически отдаёт
# 503 «service is currently unavailable» без всякой вины настроек.
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_STATUS_RE = re.compile(r"\[(\d{3})\]")


def error_status(exc: BaseException) -> int | None:
    """HTTP-код ошибки Google API, если его удаётся определить."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def is_transient(exc: BaseException) -> bool:
    """Временный сбой (стоит повторить) или постоянная проблема настроек?"""
    if error_status(exc) in TRANSIENT_STATUSES:
        return True
    if error_status(exc) is not None:
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("timed out", "timeout", "connection", "temporarily", "unavailable")
    )


def explain(exc: BaseException, account_email: str = "", spreadsheet_id: str = "") -> str:
    """Человеческое объяснение ошибки вместо общего списка причин."""
    status = error_status(exc)
    if status == 403:
        who = account_email or "сервисному аккаунту"
        return (
            f"нет доступа к таблице. Откройте её для {who} с ролью «Редактор» "
            "и убедитесь, что в Google Cloud включены Google Sheets API и Google Drive API"
        )
    if status == 404:
        return (
            f"таблица не найдена (id: {spreadsheet_id or '—'}). "
            "Проверьте GOOGLE_SPREADSHEET_ID — это часть ссылки между /d/ и /edit"
        )
    if status == 429:
        return "превышен лимит запросов Google API — бот повторит попытку позже"
    if status in TRANSIENT_STATUSES:
        return (
            f"временный сбой на стороне Google (HTTP {status}). "
            "Настройки ни при чём — попробуйте через минуту"
        )
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return str(exc)


class SheetsExporter:
    """Дописывает по строке на каждого успешно прошедшего опрос кандидата."""

    #: сколько раз повторять запрос при временных ошибках Google
    max_attempts = 3
    #: базовая пауза между попытками (удваивается)
    retry_delay = 2.0

    def __init__(self, config: SheetsConfig, steps: Sequence[Step]) -> None:
        self.config = config
        self.steps = [step for step in steps if step.in_summary]
        self._worksheet: Any = None
        self._lock = threading.Lock()
        self._failed = False
        #: последняя ошибка человеческим языком (используется в preflight)
        self.last_error: str = ""

    # ------------------------------------------------------------------ #

    def account_email(self) -> str:
        """E-mail сервисного аккаунта из файла ключа — для понятных подсказок."""
        try:
            data = json.loads(Path(self.config.credentials_file).read_text(encoding="utf-8"))
            return str(data.get("client_email", ""))
        except (OSError, ValueError):
            return ""

    def _with_retry(self, func: Callable[..., Any], *args: Any) -> Any:
        """Повторяет запрос при временных ошибках Google (503, 429, таймауты)."""
        delay = self.retry_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                return func(*args)
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_attempts or not is_transient(exc):
                    raise
                logger.warning(
                    "Google Sheets: временная ошибка (%s), повтор %d из %d через %.0f с",
                    explain(exc, self.account_email(), self.config.spreadsheet_id),
                    attempt + 1, self.max_attempts, delay,
                )
                self._worksheet = None  # переподключимся на следующей попытке
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("недостижимо")

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

    def _open_spreadsheet(self) -> Any:
        """Авторизация по ключу сервисного аккаунта и открытие таблицы."""
        import gspread  # локальный импорт: без Sheets зависимость не нужна
        from google.oauth2.service_account import Credentials

        key_path = Path(self.config.credentials_file)
        if not key_path.exists():
            raise FileNotFoundError(
                f"Файл ключа сервисного аккаунта не найден: {key_path.resolve()}"
            )

        creds = Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
        client = gspread.authorize(creds)
        return client.open_by_key(self.config.spreadsheet_id)

    def _open_worksheet(self, spreadsheet: Any) -> Any:
        """Нужный лист таблицы: берём существующий, при отсутствии создаём."""
        title = self.config.worksheet
        try:
            worksheet = spreadsheet.worksheet(title)
        except Exception as exc:  # noqa: BLE001 — gspread.WorksheetNotFound и аналоги
            if "worksheetnotfound" not in type(exc).__name__.lower() + str(exc).lower():
                raise
            worksheet = spreadsheet.add_worksheet(
                title=title, rows=1000, cols=max(10, len(self.header()) + 2)
            )
            logger.info("В таблице создан лист '%s'", title)

        # Шапку пишем только в пустой лист — существующие данные не трогаем
        if self.config.write_header and not worksheet.get_all_values():
            worksheet.append_row(self.header(), value_input_option="USER_ENTERED")
            logger.info("В лист '%s' записана строка заголовков", title)
        return worksheet

    def _get_worksheet(self) -> Any:
        """Ленивая авторизация и получение листа (выполняется в отдельном потоке)."""
        if self._worksheet is not None:
            return self._worksheet

        with self._lock:
            if self._worksheet is not None:
                return self._worksheet

            worksheet = self._open_worksheet(self._open_spreadsheet())
            self._worksheet = worksheet
            logger.info(
                "Google Sheets подключён: таблица %s, лист '%s'",
                self.config.spreadsheet_id,
                self.config.worksheet,
            )
            return worksheet

    def _append_once(self, row: Sequence[str]) -> None:
        worksheet = self._get_worksheet()
        worksheet.append_row(list(row), value_input_option="USER_ENTERED")

    def _append_sync(self, row: Sequence[str]) -> None:
        self._with_retry(self._append_once, row)

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
            self.last_error = explain(exc, self.account_email(), self.config.spreadsheet_id)
            logger.error(
                "Не удалось выгрузить анкету %s в Google Sheets: %s", user_id, self.last_error,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            logger.info("Анкета %s сохранена в базу и отправлена менеджеру", user_id)
            self._failed = True
            return False

    async def check_connection(self) -> bool:
        """Проверка доступа к таблице при старте бота (не критичная)."""
        if not self.enabled:
            logger.info("Выгрузка в Google Sheets отключена в конфиге")
            return False
        try:
            await asyncio.to_thread(self._with_retry, self._get_worksheet)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = explain(exc, self.account_email(), self.config.spreadsheet_id)
            logger.error(
                "Google Sheets недоступен на старте (бот продолжит работу): %s", self.last_error
            )
            return False
