"""Первичная настройка: создаёт config.yaml и .env, находит ключ Google.

    python tools/configure.py

Скрипт спросит токен, чат менеджера и таблицу (можно вставить полную ссылку),
найдёт JSON-ключ сервисного аккаунта в папке проекта и подскажет, кому открыть
доступ к таблице. Повторный запуск можно использовать, чтобы поменять значения:
текущие показываются как значения по умолчанию — просто нажмите Enter.

Неинтерактивно:

    python tools/configure.py --token 123:AA... --manager 123456789 \
        --sheet https://docs.google.com/spreadsheets/d/ID/edit
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

if sys.platform == "win32":  # корректный вывод кириллицы в PowerShell
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"
CONFIG_EXAMPLE = ROOT / "config.example.yaml"
ENV = ROOT / ".env"
CREDENTIALS = ROOT / "credentials.json"

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


# --------------------------------------------------------------------------- #


def read_env() -> dict[str, str]:
    """Читает существующий .env, чтобы не потерять уже введённые значения."""
    values: dict[str, str] = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def ask(prompt: str, current: str = "", *, secret: bool = False) -> str:
    """Спрашивает значение, показывая текущее как значение по умолчанию."""
    if current:
        shown = f"{current[:12]}...{current[-4:]}" if secret and len(current) > 20 else current
        answer = input(f"{prompt}\n  [{shown}] > ").strip()
        return answer or current
    return input(f"{prompt}\n  > ").strip()


def extract_sheet_id(value: str) -> str:
    """Принимает и полную ссылку, и голый id."""
    value = value.strip()
    match = SHEET_ID_RE.search(value)
    return match.group(1) if match else value


def find_service_account_key() -> Path | None:
    """Ищет JSON сервисного аккаунта в папке проекта, Загрузках и на Рабочем столе."""
    if CREDENTIALS.exists():
        return CREDENTIALS

    candidates: list[Path] = []
    search_dirs = [ROOT, Path.home() / "Downloads", Path.home() / "Desktop"]
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json"))[:200]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if data.get("type") == "service_account" and data.get("client_email"):
                candidates.append(path)
    return candidates[0] if candidates else None


def service_account_email(path: Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("client_email", "?")
    except (OSError, ValueError):
        return "?"


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="Первичная настройка бота")
    parser.add_argument("--token", help="токен от @BotFather")
    parser.add_argument("--manager", help="ID чата менеджера")
    parser.add_argument("--sheet", help="ссылка на Google Таблицу или её ID")
    parser.add_argument("--name", help="имя, которым бот представляется в диалоге")
    parser.add_argument("--no-input", action="store_true", help="ничего не спрашивать")
    args = parser.parse_args()

    print("=" * 62)
    print("НАСТРОЙКА БОТА")
    print("=" * 62)

    # --- config.yaml ------------------------------------------------------
    if CONFIG.exists():
        print(f"\nconfig.yaml уже есть — оставляю как есть ({CONFIG}).")
    elif CONFIG_EXAMPLE.exists():
        shutil.copy(CONFIG_EXAMPLE, CONFIG)
        print(f"\nСоздан config.yaml (сценарий и тексты) — {CONFIG}")
    else:
        print("Не найден config.example.yaml — проект распакован не полностью.", file=sys.stderr)
        return 2

    # --- имя бота в диалоге ------------------------------------------------
    if args.name:
        text = CONFIG.read_text(encoding="utf-8")
        updated, count = re.subn(
            r'(?m)^(\s*name:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{args.name}"', text, count=1
        )
        if count:
            CONFIG.write_text(updated, encoding="utf-8")
            print(f"Имя бота в диалоге: {args.name}")
        else:
            print("Не нашёл строку 'name:' в config.yaml — задайте имя вручную", file=sys.stderr)

    env = read_env()
    interactive = not args.no_input

    # --- токен ------------------------------------------------------------
    token = args.token or env.get("BOT_TOKEN", "")
    if interactive and not args.token:
        print("\n1. Токен бота от @BotFather")
        print("   Вида 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        for attempt in range(3):
            token = ask("   Вставьте токен (правая кнопка мыши — вставить)", token, secret=True)
            if token:
                break
            if attempt < 2:
                print("   Пусто. Скопируйте токен из чата с @BotFather и вставьте сюда.")
    if not token:
        print(
            "\nТокен обязателен — без него бот не запустится.\n"
            "Можно передать его сразу в команде:\n"
            '   py tools\\configure.py --token "123456789:AA..."',
            file=sys.stderr,
        )
        return 2
    if not TOKEN_RE.match(token):
        print(f"Токен выглядит странно: {token[:20]}... Ожидается вид 123456789:AA...",
              file=sys.stderr)

    # --- менеджер ---------------------------------------------------------
    manager = args.manager or env.get("MANAGER_CHAT_ID", "")
    if interactive and not args.manager:
        print("\n2. Куда слать карточки кандидатов")
        print("   Можно пропустить (Enter): запустите бота, напишите ему /id,")
        print("   он ответит числом — и запустите этот скрипт ещё раз.")
        manager = ask("   ID чата менеджера", manager)

    # --- таблица ----------------------------------------------------------
    sheet_id = extract_sheet_id(args.sheet) if args.sheet else env.get("GOOGLE_SPREADSHEET_ID", "")
    if interactive and not args.sheet:
        print("\n3. Google Таблица для отчётов")
        print("   Вставьте ссылку на таблицу целиком или её ID (Enter — пропустить).")
        sheet_id = extract_sheet_id(ask("   Ссылка или ID", sheet_id))

    # --- ключ Google ------------------------------------------------------
    key_path = find_service_account_key()
    if key_path and key_path != CREDENTIALS:
        print(f"\nНайден ключ сервисного аккаунта: {key_path}")
        if interactive:
            answer = input(f"   Скопировать его как {CREDENTIALS.name}? [Y/n] > ").strip().lower()
        else:
            answer = "y"
        if answer in {"", "y", "yes", "д", "да"}:
            shutil.copy(key_path, CREDENTIALS)
            key_path = CREDENTIALS
            print(f"   Скопирован: {CREDENTIALS}")

    sheets_enabled = bool(sheet_id) and CREDENTIALS.exists()

    # --- .env -------------------------------------------------------------
    lines = [
        "# Секреты. Файл в .gitignore — в репозиторий не попадёт.",
        "# Создан автоматически: python tools/configure.py",
        "",
        f"BOT_TOKEN={token}",
        "",
        "# Куда приходят карточки кандидатов (узнать: команда /id в чате с ботом)",
        f"MANAGER_CHAT_ID={manager}",
        "",
        "# Google Sheets",
        f"GOOGLE_SHEETS_ENABLED={'true' if sheets_enabled else 'false'}",
        "GOOGLE_CREDENTIALS_FILE=credentials.json",
        f"GOOGLE_SPREADSHEET_ID={sheet_id}",
        "",
        "# Прокси, если Telegram недоступен напрямую",
        "# PROXY_URL=socks5://127.0.0.1:1082",
        "",
    ]
    ENV.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nСохранено в {ENV}")

    # --- итог --------------------------------------------------------------
    print("\n" + "=" * 62)
    print("ЧТО ПОЛУЧИЛОСЬ")
    print("=" * 62)
    print(f"  Токен:            задан ({token[:12]}...)")
    print(f"  Чат менеджера:    {manager or 'НЕ ЗАДАН — узнайте через /id'}")
    print(f"  Таблица:          {sheet_id or 'НЕ ЗАДАНА'}")
    print(f"  Ключ Google:      {'credentials.json' if CREDENTIALS.exists() else 'НЕ НАЙДЕН'}")
    print(f"  Выгрузка в Sheets:{' включена' if sheets_enabled else ' выключена'}")

    if CREDENTIALS.exists() and sheet_id:
        email = service_account_email(CREDENTIALS)
        print("\nВАЖНО: откройте таблице доступ для сервисного аккаунта —")
        print(f"  {email}")
        print("  Таблица -> Поделиться -> вставить адрес -> роль «Редактор» -> Отправить.")
        print(f"  Таблица: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")

    print("\nДальше:")
    print("  python tools/preflight.py     # проверить токен, менеджера, таблицу")
    print("  python -m bot                 # запустить бота")
    return 0


if __name__ == "__main__":
    sys.exit(main())
