"""Прогон диалога в терминале — без Telegram, токена и прокси.

Нужен, чтобы заказчик мог вычитать тексты и проверить логику сценария:

    python tools/simulate.py                    # интерактивно
    echo "да\nИванов Иван Иванович\n35\nнет\nнет" | python tools/simulate.py
    python tools/simulate.py --config other.yaml --fast

Данные никуда не сохраняются: SQLite и Google Sheets в этом режиме отключены.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import ConfigError, load_config  # noqa: E402
from bot.handlers import SurveyBot  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


class ConsoleBot:
    """Заглушка telegram.Bot: печатает сообщения в терминал."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id == "manager":
            print(f"\n{YELLOW}┌─ Уведомление менеджеру ─────────────────{RESET}")
            for line in text.splitlines():
                print(f"{YELLOW}│{RESET} {line}")
            print(f"{YELLOW}└─────────────────────────────────────────{RESET}")
        else:
            print(f"\n{GREEN}{self.name}:{RESET} {text}")
            if reply_markup is not None and getattr(reply_markup, "keyboard", None):
                buttons = " | ".join(
                    btn.text if hasattr(btn, "text") else str(btn)
                    for row in reply_markup.keyboard for btn in row
                )
                print(f"{DIM}   [ {buttons} ]{RESET}")
        return SimpleNamespace(message_id=0)

    async def send_chat_action(self, chat_id, action, **kwargs):
        print(f"{DIM}   {self.name} печатает...{RESET}", end="\r", flush=True)
        return True


class ConsoleContext:
    def __init__(self, bot: ConsoleBot) -> None:
        self.bot = bot
        self.user_data: dict = {}
        self.chat_data: dict = {}
        self.bot_data: dict = {}
        self.error = None


def make_update(text: str):
    user = SimpleNamespace(id=1000, username="test_user", first_name="Тест")
    chat = SimpleNamespace(id=1000, type="private")
    message = SimpleNamespace(text=text, caption=None, message_id=1, chat=chat)
    return SimpleNamespace(
        effective_user=user, effective_chat=chat, effective_message=message, message=message
    )


async def run(config_path: str | None, fast: bool) -> int:
    try:
        config = load_config(config_path) if config_path else load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    # В симуляции ничего не пишем наружу.
    config.database.enabled = False
    config.sheets.enabled = False
    config.manager_chat_id = "manager"
    if fast:
        config.typing.enabled = False

    survey = SurveyBot(config, storage=None, sheets=None)
    bot = ConsoleBot(config.bot_name)
    context = ConsoleContext(bot)

    print(f"{BOLD}=== Симуляция диалога ({len(config.survey)} шагов) ==={RESET}")
    print(f"{DIM}Отвечайте как обычный пользователь. /cancel — прервать, Ctrl+C — выход.{RESET}")

    state = await survey.cmd_start(make_update("/start"), context)

    while state is not None and isinstance(state, int):
        try:
            answer = input(f"\n{BOLD}Вы:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            return 0
        if not answer:
            continue
        if answer in {"/cancel", "/stop"}:
            await survey.cmd_cancel(make_update(answer), context)
            break
        if answer == "/start":
            state = await survey.cmd_start(make_update(answer), context)
            continue
        state = await survey.make_step_handler(state)(make_update(answer), context)

    print(f"\n{BOLD}=== Диалог завершён ==={RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон диалога бота в терминале")
    parser.add_argument("--config", help="путь к конфигу (по умолчанию config.yaml)")
    parser.add_argument("--fast", action="store_true", help="без пауз «печатает...»")
    parser.add_argument("--debug", action="store_true", help="показывать логи бота")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    return asyncio.run(run(args.config, args.fast))


if __name__ == "__main__":
    sys.exit(main())
