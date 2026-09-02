"""Проверка всего окружения перед запуском бота.

    python tools/preflight.py

Проверяет по очереди:
  1. конфиг и токен          — getMe через Telegram API (с учётом прокси);
  2. чат менеджера           — реально отправляет туда тестовое сообщение;
  3. Google Sheets           — подключается к таблице и дописывает тестовую строку;
  4. SQLite                  — создаёт файл базы и пишет пробную запись.

Каждая проверка независима: если что-то не настроено, скрипт скажет,
что именно и как починить.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":  # корректный вывод кириллицы в PowerShell
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


from bot.config import Config, ConfigError, load_config  # noqa: E402
from bot.sheets import SheetsExporter  # noqa: E402
from bot.storage import CandidateStorage  # noqa: E402

OK, FAIL, SKIP = "  [ OK ]", "  [ОШИБКА]", "  [ПРОПУСК]"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if Path(".env").exists():
        load_dotenv(Path(".env"))


def _title(text: str) -> None:
    print(f"\n{text}\n" + "-" * len(text))


# --------------------------------------------------------------------------- #


async def check_telegram(config: Config) -> object | None:
    """Проверяет токен и возвращает готовый Bot (или None)."""
    _title("1. Telegram API")
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError
    from telegram.request import HTTPXRequest

    kwargs = {"connect_timeout": 20.0, "read_timeout": 20.0}
    if config.proxy:
        kwargs["proxy"] = config.proxy
        print(f"  прокси: {config.proxy}")
    else:
        print("  прокси: не используется (прямое подключение)")

    bot = Bot(token=config.token, request=HTTPXRequest(**kwargs))
    try:
        await bot.initialize()
        me = await bot.get_me()
    except InvalidToken:
        print(f"{FAIL} Telegram отклонил токен. Проверьте BOT_TOKEN в .env")
        return None
    except TelegramError as exc:
        print(f"{FAIL} нет связи с Telegram: {exc}")
        print("       Если Telegram заблокирован — укажите PROXY_URL в .env")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} нет связи с Telegram: {exc}")
        if config.proxy:
            print(f"       Проверьте, работает ли прокси {config.proxy}")
        else:
            print("       Если Telegram заблокирован — укажите PROXY_URL в .env")
        return None

    print(f"{OK} бот @{me.username} (id {me.id}), имя в диалоге: «{config.bot_name}»")
    return bot


async def check_manager(config: Config, bot) -> bool:
    """Пробует отправить тестовое сообщение менеджеру."""
    _title("2. Чат менеджера")
    if bot is None:
        print(f"{SKIP} нет связи с Telegram")
        return False
    if not config.manager_chat_id:
        print(f"{FAIL} MANAGER_CHAT_ID не задан — уведомления о кандидатах отправляться не будут")
        print("       Напишите боту команду /id, он ответит числом. Впишите его в .env")
        return False

    from telegram.error import TelegramError

    target: str | int = config.manager_chat_id
    if isinstance(target, str) and not target.startswith("@"):
        try:
            target = int(target)
        except ValueError:
            pass

    try:
        await bot.send_message(
            chat_id=target,
            text="Проверка связи: сюда будут приходить карточки кандидатов. "
                 "Это сообщение можно удалить.",
        )
    except TelegramError as exc:
        print(f"{FAIL} не удалось отправить сообщение в {target}: {exc}")
        print("       Менеджер должен сам написать боту /start (иначе Telegram запрещает")
        print("       писать первым), а для группы — добавьте бота в неё.")
        return False

    print(f"{OK} тестовое сообщение отправлено в чат {target}")
    return True


async def check_sheets(config: Config) -> bool:
    _title("3. Google Sheets")
    if not config.sheets.enabled:
        print(f"{SKIP} выгрузка выключена (google_sheets.enabled: false)")
        return False

    key_path = Path(config.sheets.credentials_file)
    if not key_path.exists():
        print(f"{FAIL} файл ключа не найден: {key_path.resolve()}")
        print("       Положите JSON сервисного аккаунта рядом с ботом как credentials.json")
        return False
    if not config.sheets.spreadsheet_id:
        print(f"{FAIL} не задан GOOGLE_SPREADSHEET_ID")
        print("       Создать таблицу: python tools/create_sheet.py --share ваша@почта.com")
        return False

    exporter = SheetsExporter(config.sheets, config.survey)
    print(f"  таблица: {config.sheets.spreadsheet_id}, лист: «{config.sheets.worksheet}»")
    print(f"  колонки: {', '.join(exporter.header())}")

    if not await exporter.check_connection():
        print(f"{FAIL} подключиться не удалось. Частые причины:")
        print("       • сервисному аккаунту не выдан доступ к таблице (роль «Редактор»);")
        print("       • не включены Google Sheets API и Google Drive API в проекте;")
        print("       • неверный spreadsheet_id (часть URL между /d/ и /edit).")
        return False

    demo = {step.key: "тест" for step in config.survey if step.in_summary}
    if not await exporter.append(
        answers=demo, username="@test", user_id=0,
        status="ТЕСТ (можно удалить)", created_at=datetime.now(),
    ):
        print(f"{FAIL} подключение есть, но записать строку не удалось")
        return False

    print(f"{OK} тестовая строка добавлена в таблицу (удалите её вручную)")
    return True


async def check_database(config: Config) -> bool:
    _title("4. База данных")
    if not config.database.enabled:
        print(f"{SKIP} SQLite выключен (database.enabled: false)")
        return False

    storage = CandidateStorage(config.database.path, config.survey, enabled=True)
    storage.init()
    if not storage._ready:  # noqa: SLF001
        print(f"{FAIL} не удалось открыть базу {config.database.path}")
        return False

    total = await storage.count()
    print(f"{OK} база {Path(config.database.path).resolve()} доступна, анкет: {total}")
    return True


# --------------------------------------------------------------------------- #


async def run() -> int:
    _load_dotenv()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    print("=" * 60)
    print("ПРОВЕРКА НАСТРОЕК БОТА")
    print("=" * 60)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"\n{FAIL} конфигурация: {exc}")
        return 2

    print(f"\n  конфиг:   {Path('config.yaml').resolve()}")
    print(f"  сценарий: {len(config.survey)} шагов — "
          f"{', '.join(step.key for step in config.survey)}")

    bot = await check_telegram(config)
    manager_ok = await check_manager(config, bot)
    sheets_ok = await check_sheets(config)
    db_ok = await check_database(config)

    if bot is not None:
        await bot.shutdown()

    _title("ИТОГ")
    print(f"  Telegram:      {'готов' if bot else 'НЕ ГОТОВ — бот не запустится'}")
    print(f"  Менеджер:      {'готов' if manager_ok else 'не настроен — уведомлений не будет'}")
    print(f"  Google Sheets: {'готов' if sheets_ok else 'не настроен — выгрузки не будет'}")
    print(f"  SQLite:        {'готов' if db_ok else 'не настроен'}")

    if bot is None:
        print("\nИсправьте подключение к Telegram и запустите проверку снова.")
        return 1

    print("\nМожно запускать:  python -m bot")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
