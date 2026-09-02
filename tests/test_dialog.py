"""Тесты полного сценария диалога на заглушках Telegram."""

from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from bot.handlers import SurveyBot
from bot.sheets import SheetsExporter
from bot.storage import CandidateStorage
from tests.conftest import make_update

# asyncio_mode=auto (pytest.ini): async-тесты подхватываются автоматически

MANAGER_CHAT = 555000111


@pytest.fixture
def survey(config, tmp_path):
    config.database.path = str(tmp_path / "candidates.db")
    storage = CandidateStorage(config.database.path, config.survey, enabled=True)
    storage.init()
    sheets = SheetsExporter(config.sheets, config.survey)
    return SurveyBot(config, storage=storage, sheets=sheets)


async def run_step(survey: SurveyBot, state: int, text: str, context):
    """Отдаёт ответ пользователя обработчику текущего состояния."""
    handler = survey.make_step_handler(state)
    return await handler(make_update(text), context)


# --------------------------------------------------------------------------- #
#  Успешный сценарий
# --------------------------------------------------------------------------- #

async def test_full_successful_dialog(survey, bot_and_context):
    bot, context = bot_and_context

    state = await survey.cmd_start(make_update("/start"), context)
    assert state == 0
    assert "Григорий" in bot.last          # бот представился
    assert bot.messages[-1]["markup"] is not None  # с кнопками Да/Нет

    state = await run_step(survey, state, "Да", context)
    assert state == 1

    state = await run_step(survey, state, "Иванов Иван Иванович", context)
    assert state == 2

    state = await run_step(survey, state, "35", context)
    assert state == 3

    state = await run_step(survey, state, "Нет", context)
    assert state == 4

    state = await run_step(survey, state, "Нет", context)
    assert state == ConversationHandler.END

    user_texts = "\n".join(bot.texts_for(42))
    assert "ФИО: Иванов Иван Иванович" in user_texts
    assert "Возраст: 35 лет" in user_texts
    assert "Здоровье: Нет проблем" in user_texts
    assert "Судимости: Нет судимостей" in user_texts

    # Уведомление менеджеру
    manager_texts = bot.texts_for(MANAGER_CHAT)
    assert len(manager_texts) == 1
    notification = manager_texts[0]
    assert "Новый кандидат" in notification
    assert "Иванов Иван Иванович" in notification
    assert "Возраст: 35" in notification and "35 лет" not in notification
    assert "@ivanov" in notification
    assert "ID: 42" in notification

    # Анкета сохранена
    assert await survey.storage.count() == 1
    # Сессия очищена
    assert context.user_data == {}


# --------------------------------------------------------------------------- #
#  Отказы
# --------------------------------------------------------------------------- #

async def test_not_interested_ends_dialog(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    state = await run_step(survey, state, "Нет", context)

    assert state == ConversationHandler.END
    assert bot.texts_for(MANAGER_CHAT) == []


async def test_age_above_limit_rejects(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    state = await run_step(survey, state, "Да", context)
    state = await run_step(survey, state, "Иванов Иван Иванович", context)
    state = await run_step(survey, state, "70", context)

    assert state == ConversationHandler.END
    assert "63" in bot.last
    assert bot.texts_for(MANAGER_CHAT) == []


async def test_health_problem_rejects(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    state = await run_step(survey, state, "Да", context)
    state = await run_step(survey, state, "Иванов Иван Иванович", context)
    state = await run_step(survey, state, "35", context)
    state = await run_step(survey, state, "Да", context)

    assert state == ConversationHandler.END
    assert bot.texts_for(MANAGER_CHAT) == []


async def test_criminal_record_rejects(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    for answer in ("Да", "Иванов Иван Иванович", "35", "Нет"):
        state = await run_step(survey, state, answer, context)
    state = await run_step(survey, state, "Да", context)

    assert state == ConversationHandler.END
    assert bot.texts_for(MANAGER_CHAT) == []


# --------------------------------------------------------------------------- #
#  Некорректный ввод
# --------------------------------------------------------------------------- #

async def test_invalid_yes_no_keeps_state(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)

    state = await run_step(survey, state, "может быть", context)
    assert state == 0                                   # состояние не изменилось
    assert "да" in bot.last.lower() and "нет" in bot.last.lower()
    assert bot.messages[-1]["markup"] is not None       # кнопки на месте

    state = await run_step(survey, state, "Да", context)
    assert state == 1


async def test_short_name_asks_again(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    state = await run_step(survey, state, "Да", context)

    state = await run_step(survey, state, "Ив", context)
    assert state == 1

    state = await run_step(survey, state, "Иванов Иван Иванович", context)
    assert state == 2


async def test_non_numeric_age_asks_again(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    state = await run_step(survey, state, "Да", context)
    state = await run_step(survey, state, "Иванов Иван Иванович", context)

    state = await run_step(survey, state, "чуть за тридцать, наверное", context)
    assert state == 2
    assert context.user_data["answers"].get("age") is None

    state = await run_step(survey, state, "35", context)
    assert state == 3


# --------------------------------------------------------------------------- #
#  Отмена и устойчивость
# --------------------------------------------------------------------------- #

async def test_cancel_clears_session(survey, bot_and_context):
    bot, context = bot_and_context
    state = await survey.cmd_start(make_update("/start"), context)
    await run_step(survey, state, "Да", context)

    result = await survey.cmd_cancel(make_update("/cancel"), context)
    assert result == ConversationHandler.END
    assert context.user_data == {}
    assert "/start" in bot.last


async def test_manager_notification_failure_does_not_break_dialog(survey, bot_and_context):
    bot, context = bot_and_context
    bot.fail_chats.add(MANAGER_CHAT)  # имитируем "Chat not found"

    state = await survey.cmd_start(make_update("/start"), context)
    for answer in ("Да", "Иванов Иван Иванович", "35", "Нет", "Нет"):
        state = await run_step(survey, state, answer, context)

    assert state == ConversationHandler.END
    assert any("резюмируем" in t.lower() or "ФИО" in t for t in bot.texts_for(42))
    assert await survey.storage.count() == 1


async def test_two_users_do_not_interfere(survey, bot_and_context):
    from tests.conftest import FakeContext

    bot, context_a = bot_and_context
    context_b = FakeContext(bot)

    state_a = await survey.cmd_start(make_update("/start", user_id=1, username="a"), context_a)
    state_b = await survey.cmd_start(make_update("/start", user_id=2, username="b"), context_b)

    handler = survey.make_step_handler(state_a)
    state_a = await handler(make_update("Да", user_id=1, username="a"), context_a)
    state_b = await survey.make_step_handler(state_b)(
        make_update("Да", user_id=2, username="b"), context_b
    )

    await survey.make_step_handler(state_a)(
        make_update("Первый Первый Первый", user_id=1, username="a"), context_a
    )
    await survey.make_step_handler(state_b)(
        make_update("Второй Второй Второй", user_id=2, username="b"), context_b
    )

    assert context_a.user_data["answers"]["full_name"] == "Первый Первый Первый"
    assert context_b.user_data["answers"]["full_name"] == "Второй Второй Второй"


async def test_typing_action_is_sent_when_enabled(survey, bot_and_context):
    bot, context = bot_and_context
    survey.config.typing.enabled = True
    survey.config.typing.min_delay = 0.0
    survey.config.typing.max_delay = 0.0
    survey.config.typing.per_char = 0.0

    await survey.cmd_start(make_update("/start"), context)
    assert bot.actions, "Бот должен показывать статус «печатает...»"


async def test_id_command_reports_chat_id(survey, bot_and_context):
    """Команда /id нужна менеджеру, чтобы узнать MANAGER_CHAT_ID."""
    bot, context = bot_and_context
    await survey.cmd_id(make_update("/id", user_id=777, chat_id=777), context)
    assert "777" in bot.last
    assert "MANAGER_CHAT_ID" in bot.last
