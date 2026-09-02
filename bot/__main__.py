"""Точка входа: запуск бота в режиме long polling.

    python -m bot                     # конфиг config.yaml
    CONFIG_PATH=other.yaml python -m bot
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, Defaults
from telegram.error import InvalidToken, NetworkError, TelegramError
from telegram.request import HTTPXRequest

from .config import Config, ConfigError, load_config
from .handlers import SurveyBot
from .logging_setup import setup_logging
from .sheets import SheetsExporter
from .storage import CandidateStorage

logger = logging.getLogger("bot")


def _load_dotenv() -> None:
    """Подхватывает .env, если установлен python-dotenv."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)


def build_application(config: Config, survey: SurveyBot) -> Application:
    """Создаёт Application с учётом прокси и таймаутов."""
    request_kwargs: dict = {
        "connect_timeout": 20.0,
        "read_timeout": 20.0,
        "write_timeout": 20.0,
        "pool_timeout": 20.0,
    }
    if config.proxy:
        logger.info("Подключение к Telegram API через прокси: %s", config.proxy)
        request_kwargs["proxy"] = config.proxy
    else:
        logger.info("Подключение к Telegram API напрямую (прокси не задан)")

    builder = (
        ApplicationBuilder()
        .token(config.token)
        .defaults(Defaults(block=False))
        .request(HTTPXRequest(**request_kwargs))
        .get_updates_request(HTTPXRequest(**{**request_kwargs, "read_timeout": 40.0}))
    )

    application = builder.build()
    survey.register(application)
    return application


async def _post_init(application: Application) -> None:
    """Служебные действия после инициализации: меню команд, проверка Sheets."""
    survey: SurveyBot = application.bot_data["survey"]
    sheets: SheetsExporter | None = application.bot_data.get("sheets")

    try:
        me = await application.bot.get_me()
        logger.info("Авторизация успешна: @%s (id %s)", me.username, me.id)
    except TelegramError as exc:
        logger.error("Не удалось получить информацию о боте: %s", exc)

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Начать диалог"),
                BotCommand("cancel", "Прервать диалог"),
                BotCommand("id", "Показать ID этого чата"),
            ]
        )
    except TelegramError as exc:
        logger.warning("Не удалось установить меню команд: %s", exc)

    if sheets is not None and sheets.enabled:
        await sheets.check_connection()

    if not survey.config.manager_chat_ids:
        logger.warning("manager_chat_id не задан: уведомления менеджеру отправляться не будут")


async def _post_shutdown(application: Application) -> None:  # noqa: ARG001
    logger.info("Бот остановлен")


def main() -> int:
    _load_dotenv()

    # --- конфиг ----------------------------------------------------------
    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging("INFO", None)
        logger.error("Ошибка конфигурации: %s", exc)
        return 2

    setup_logging(config.logging.level, config.logging.file)
    logger.info("Загружен сценарий из %d шагов: %s",
                len(config.survey), ", ".join(step.key for step in config.survey))

    # --- хранилища --------------------------------------------------------
    storage = CandidateStorage(
        config.database.path, config.survey, enabled=config.database.enabled
    )
    storage.init()

    sheets = SheetsExporter(config.sheets, config.survey, config.columns)

    survey = SurveyBot(config, storage=storage, sheets=sheets)

    # --- приложение -------------------------------------------------------
    try:
        application = build_application(config, survey)
    except InvalidToken:
        logger.error("Telegram отклонил токен бота. Проверьте BOT_TOKEN.")
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось создать приложение: %s", exc)
        if config.proxy:
            logger.error(
                "Возможно, прокси %s недоступен. Запустите прокси или уберите его из конфига.",
                config.proxy,
            )
        return 3

    application.bot_data["survey"] = survey
    application.bot_data["sheets"] = sheets
    application.post_init = _post_init
    application.post_shutdown = _post_shutdown

    logger.info("Бот запущен!")
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
    except InvalidToken:
        logger.error("Telegram отклонил токен бота. Проверьте BOT_TOKEN.")
        return 2
    except NetworkError as exc:
        logger.error("Сетевая ошибка при подключении к Telegram: %s", exc)
        if config.proxy:
            logger.error("Проверьте, работает ли прокси %s", config.proxy)
        else:
            logger.error(
                "Если Telegram заблокирован в вашей сети, укажите прокси: "
                "bot.proxy в config.yaml или переменную PROXY_URL"
            )
        return 3
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Получен сигнал остановки (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
