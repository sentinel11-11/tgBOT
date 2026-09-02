"""Проверка доступа к Google Sheets до запуска бота.

    python tools/check_sheets.py

Скрипт читает конфиг, подключается к таблице, дописывает тестовую строку
и сообщает, что именно не так, если подключение не удалось.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import ConfigError, load_config  # noqa: E402
from bot.sheets import SheetsExporter  # noqa: E402


async def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if not config.sheets.enabled:
        print(
            "Выгрузка в Google Sheets выключена.\n"
            "Включите её: config.yaml -> google_sheets.enabled: true"
        )
        return 1

    exporter = SheetsExporter(config.sheets, config.survey)
    print(f"Таблица:   {config.sheets.spreadsheet_id}")
    print(f"Лист:      {config.sheets.worksheet}")
    print(f"Ключ:      {Path(config.sheets.credentials_file).resolve()}")
    print(f"Колонки:   {', '.join(exporter.header())}\n")

    if not await exporter.check_connection():
        print(
            "\nНе удалось подключиться. Частые причины:\n"
            "  • сервисному аккаунту не выдан доступ к таблице (роль «Редактор»);\n"
            "  • в проекте Google Cloud не включены Google Sheets API и Google Drive API;\n"
            "  • неверный spreadsheet_id (это часть URL между /d/ и /edit);\n"
            "  • путь к JSON-ключу указан неправильно.",
            file=sys.stderr,
        )
        return 3

    demo = {step.key: "тест" for step in config.survey if step.in_summary}
    ok = await exporter.append(
        answers=demo,
        username="@test",
        user_id=0,
        status="ТЕСТ (можно удалить)",
        created_at=datetime.now(),
    )
    if ok:
        print("Готово: тестовая строка добавлена в таблицу. Удалите её вручную.")
        return 0

    print("Подключение есть, но записать строку не удалось (см. лог выше).", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
