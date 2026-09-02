"""Кому уходит карточка кандидата: получатели уведомлений."""

from __future__ import annotations

import logging

from bot.handlers import SurveyBot
from tests.conftest import make_update

# asyncio_mode=auto (pytest.ini): async-тесты подхватываются автоматически


def test_manager_chat_ids_parsing(config):
    """Один получатель или несколько — через запятую или точку с запятой."""
    config.manager_chat_id = "1216829906"
    assert config.manager_chat_ids == ["1216829906"]

    config.manager_chat_id = "1216829906, -1001234567890 ; @hr_channel"
    assert config.manager_chat_ids == ["1216829906", "-1001234567890", "@hr_channel"]

    config.manager_chat_id = None
    assert config.manager_chat_ids == []

    config.manager_chat_id = "  "
    assert config.manager_chat_ids == []


async def test_notification_goes_to_managers_not_to_candidate(config, bot_and_context):
    """Карточка уходит менеджерам и никогда — самому кандидату."""
    config.manager_chat_id = "111, -100222"
    fake_bot, context = bot_and_context
    survey = SurveyBot(config)

    update = make_update("готово", user_id=999, username="candidate")
    await survey.notify_manager(update, context, {"full_name": "Иванов Иван", "age": 27})

    recipients = [message["chat_id"] for message in fake_bot.messages]
    assert recipients == [111, -100222], "оба получателя из настройки"
    assert 999 not in recipients, "кандидат не должен получать карточку"

    text = fake_bot.messages[0]["text"]
    assert "Иванов Иван" in text
    assert "@candidate" in text


async def test_group_chat_id_is_supported(config, bot_and_context):
    """Отрицательный id (группа) передаётся числом, а не строкой."""
    config.manager_chat_id = "-1001234567890"
    fake_bot, context = bot_and_context
    survey = SurveyBot(config)

    await survey.notify_manager(
        make_update("готово", user_id=999), context, {"full_name": "Иванов", "age": 30}
    )

    assert fake_bot.messages[0]["chat_id"] == -1001234567890


async def test_channel_username_is_supported(config, bot_and_context):
    """@имя_канала остаётся строкой."""
    config.manager_chat_id = "@hr_channel"
    fake_bot, context = bot_and_context
    survey = SurveyBot(config)

    await survey.notify_manager(
        make_update("готово", user_id=999), context, {"full_name": "Иванов", "age": 30}
    )

    assert fake_bot.messages[0]["chat_id"] == "@hr_channel"


async def test_one_failed_recipient_does_not_block_others(config, bot_and_context, caplog):
    """Менеджер не нажал /start — остальные всё равно получают карточку."""
    config.manager_chat_id = "111,222"
    fake_bot, context = bot_and_context
    fake_bot.fail_chats.add(111)
    survey = SurveyBot(config)

    with caplog.at_level(logging.ERROR):
        await survey.notify_manager(
            make_update("готово", user_id=999), context, {"full_name": "Иванов", "age": 30}
        )

    assert [m["chat_id"] for m in fake_bot.messages] == [222]
    assert "начинал диалог с ботом" in caplog.text, "подсказка о причине в логе"


async def test_no_manager_configured_is_only_a_warning(config, bot_and_context, caplog):
    """Без получателя диалог не ломается — только предупреждение в лог."""
    config.manager_chat_id = None
    fake_bot, context = bot_and_context
    survey = SurveyBot(config)

    with caplog.at_level(logging.WARNING):
        await survey.notify_manager(
            make_update("готово", user_id=999), context, {"full_name": "Иванов", "age": 30}
        )

    assert fake_bot.messages == []
    assert "manager_chat_id не задан" in caplog.text
