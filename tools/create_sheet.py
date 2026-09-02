"""Создание Google Таблицы для отчётов силами сервисного аккаунта.

Нужен, если готовой таблицы ещё нет:

    python tools/create_sheet.py --share ваша@почта.com

Скрипт создаст таблицу, добавит строку заголовков по текущему сценарию,
выдаст вам права редактора и напечатает строку для .env.

Если таблица уже есть — создавать ничего не надо: просто откройте к ней доступ
сервисному аккаунту (его e-mail печатает `--whoami`) и укажите её id в .env.

    python tools/create_sheet.py --whoami
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import ConfigError, load_config  # noqa: E402
from bot.sheets import SCOPES, SheetsExporter  # noqa: E402

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if Path(".env").exists():
        load_dotenv(Path(".env"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать Google Таблицу для отчётов бота")
    parser.add_argument("--title", default="Кандидаты — заявки из Telegram", help="название таблицы")
    parser.add_argument("--share", metavar="EMAIL", help="кому выдать доступ редактора")
    parser.add_argument("--whoami", action="store_true", help="показать e-mail сервисного аккаунта")
    args = parser.parse_args()

    _load_dotenv()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    key_path = Path(config.sheets.credentials_file)
    if not key_path.exists():
        print(
            f"Файл ключа сервисного аккаунта не найден: {key_path.resolve()}\n"
            "Положите JSON из Google Cloud рядом с ботом как credentials.json\n"
            "(или укажите путь в GOOGLE_CREDENTIALS_FILE).",
            file=sys.stderr,
        )
        return 2

    account_email = json.loads(key_path.read_text(encoding="utf-8")).get("client_email", "?")
    print(f"Сервисный аккаунт: {account_email}")
    if args.whoami:
        print("\nОткройте доступ к своей таблице этому адресу (роль «Редактор»),")
        print("затем впишите id таблицы в .env:  GOOGLE_SPREADSHEET_ID=...")
        return 0

    import gspread
    from google.oauth2.service_account import Credentials

    scopes = DRIVE_SCOPES if args.share else SCOPES
    client = gspread.authorize(Credentials.from_service_account_file(str(key_path), scopes=scopes))

    try:
        spreadsheet = client.create(args.title)
    except Exception as exc:  # noqa: BLE001
        print(f"\nНе удалось создать таблицу: {exc}", file=sys.stderr)
        print(
            "Проверьте, что в проекте Google Cloud включены Google Sheets API и Google Drive API.",
            file=sys.stderr,
        )
        return 3

    exporter = SheetsExporter(config.sheets, config.survey)
    worksheet = spreadsheet.sheet1
    worksheet.update_title(config.sheets.worksheet)
    worksheet.append_row(exporter.header(), value_input_option="USER_ENTERED")
    worksheet.format("A1:Z1", {"textFormat": {"bold": True}})
    worksheet.freeze(rows=1)

    if args.share:
        try:
            spreadsheet.share(args.share, perm_type="user", role="writer", notify=False)
            print(f"Доступ редактора выдан: {args.share}")
        except Exception as exc:  # noqa: BLE001
            print(f"Таблица создана, но поделиться не удалось: {exc}", file=sys.stderr)

    print("\nГотово!")
    print(f"  Название: {args.title}")
    print(f"  Ссылка:   https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit")
    print("\nДобавьте в .env:")
    print("  GOOGLE_SHEETS_ENABLED=true")
    print(f"  GOOGLE_SPREADSHEET_ID={spreadsheet.id}")
    print("\nПотом проверьте:  python tools/preflight.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
