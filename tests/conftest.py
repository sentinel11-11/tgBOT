"""Общие фикстуры и заглушки Telegram-объектов для тестов."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VALID_TOKEN = "123456789:AAFakeTokenForTestsOnly_1234567890abcd"


@pytest.fixture
def example_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.example.yaml"


@pytest.fixture
def config(example_config_path, monkeypatch):
    from bot.config import load_config

    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("MANAGER_CHAT_ID", "555000111")
    monkeypatch.delenv("PROXY_URL", raising=False)
    cfg = load_config(example_config_path)
    cfg.typing.enabled = False  # без пауз в тестах
    return cfg


# --------------------------------------------------------------------------- #
#  Заглушки Telegram
# --------------------------------------------------------------------------- #


class FakeBot:
    """Записывает всё, что бот попытался отправить."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.actions: list[Any] = []
        self.fail_chats: set[Any] = set()

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if chat_id in self.fail_chats:
            from telegram.error import BadRequest

            raise BadRequest("Chat not found")
        self.messages.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return SimpleNamespace(message_id=len(self.messages), text=text)

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append((chat_id, action))
        return True

    # --- помощники для проверок ---
    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]

    def texts_for(self, chat_id) -> list[str]:
        return [m["text"] for m in self.messages if m["chat_id"] == chat_id]

    @property
    def last(self) -> str:
        return self.texts[-1] if self.messages else ""


class FakeContext:
    """Минимальный аналог ContextTypes.DEFAULT_TYPE."""

    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.user_data: dict[str, Any] = {}
        self.chat_data: dict[str, Any] = {}
        self.bot_data: dict[str, Any] = {}
        self.error: BaseException | None = None


def make_update(text: str, *, user_id: int = 42, username: str | None = "ivanov", chat_id=None):
    """Создаёт объект, достаточно похожий на telegram.Update для наших обработчиков."""
    chat_id = chat_id if chat_id is not None else user_id
    user = SimpleNamespace(id=user_id, username=username, first_name="Иван")
    chat = SimpleNamespace(id=chat_id, type="private")
    message = SimpleNamespace(text=text, caption=None, message_id=1, chat=chat)
    return SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        effective_message=message,
        message=message,
    )


@pytest.fixture
def bot_and_context():
    fake_bot = FakeBot()
    return fake_bot, FakeContext(fake_bot)
