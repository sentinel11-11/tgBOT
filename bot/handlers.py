"""Диалог с кандидатом: ConversationHandler, собранный из конфига.

Состояния = индексы шагов в списке `survey`. Добавление нового вопроса в
конфиг автоматически добавляет новое состояние — код править не нужно.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import Config, Step
from .keyboards import remove_keyboard, yes_no_keyboard
from .sheets import SheetsExporter
from .storage import CandidateStorage
from .texts import render, typing_delay
from .validators import bind_buttons, validate

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, Any]]

# Ключи user_data
ANSWERS = "answers"
RAW = "raw_answers"
STARTED_AT = "started_at"
RATE = "rate_hits"


class SurveyBot:
    """Собирает и обслуживает диалог по описанию из конфига."""

    def __init__(
        self,
        config: Config,
        storage: CandidateStorage | None = None,
        sheets: SheetsExporter | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.sheets = sheets
        bind_buttons(config.buttons)

        try:
            self._tz = ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Неизвестный часовой пояс '%s', использую системный", config.timezone)
            self._tz = None

    # ------------------------------------------------------------------ #
    #  Утилиты
    # ------------------------------------------------------------------ #

    def now(self) -> datetime:
        return datetime.now(self._tz) if self._tz else datetime.now()

    def context_values(self, context: ContextTypes.DEFAULT_TYPE, step: Step | None = None) -> dict:
        """Значения для подстановки в тексты: vars + имя бота + уже собранные ответы."""
        values: dict[str, Any] = dict(self.config.vars)
        values["bot_name"] = self.config.bot_name
        values.update(context.user_data.get(ANSWERS, {}))
        if step is not None:
            values["step_min"] = step.min if step.min is not None else ""
            values["step_max"] = step.max if step.max is not None else ""
        return values

    def render(self, variants, context: ContextTypes.DEFAULT_TYPE, step: Step | None = None) -> str:
        return render(
            variants,
            self.context_values(context, step),
            emoji=self.config.emoji,
        )

    async def reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        keyboard: ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
    ) -> Message | None:
        """Отправляет сообщение с имитацией набора текста («печатает...»)."""
        if not text:
            return None
        chat = update.effective_chat
        if chat is None:
            return None

        typing = self.config.typing
        if typing.enabled:
            try:
                await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
            except TelegramError as exc:
                logger.debug("Не удалось отправить chat_action: %s", exc)
            await asyncio.sleep(
                typing_delay(
                    text,
                    min_delay=typing.min_delay,
                    max_delay=typing.max_delay,
                    per_char=typing.per_char,
                    cap=typing.max_delay_cap,
                )
            )

        try:
            return await context.bot.send_message(
                chat_id=chat.id, text=text, reply_markup=keyboard
            )
        except TelegramError as exc:
            logger.error("Не удалось отправить сообщение в чат %s: %s", chat.id, exc)
            return None

    def keyboard_for(self, step: Step) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
        if step.type == "yes_no":
            return yes_no_keyboard(self.config.buttons)
        return remove_keyboard()

    def rate_limited(self, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Простой скользящий счётчик сообщений на пользователя."""
        limits = self.config.rate_limit
        if not limits.enabled:
            return False
        hits: deque = context.user_data.setdefault(RATE, deque(maxlen=limits.max_messages * 4))
        now = time.monotonic()
        while hits and now - hits[0] > limits.per_seconds:
            hits.popleft()
        hits.append(now)
        return len(hits) > limits.max_messages

    # ------------------------------------------------------------------ #
    #  Шаги
    # ------------------------------------------------------------------ #

    async def ask(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, index: int
    ) -> int:
        """Задаёт вопрос шага `index` и возвращает соответствующее состояние."""
        step = self.config.survey[index]
        text = self.render(step.questions, context, step)
        await self.reply(update, context, text, self.keyboard_for(step))
        if self.config.logging.log_steps:
            user = update.effective_user
            logger.info(
                "Шаг '%s' задан пользователю %s (@%s)",
                step.key,
                getattr(user, "id", "?"),
                getattr(user, "username", None),
            )
        return index

    def make_step_handler(self, index: int) -> Handler:
        """Создаёт обработчик ответа для шага с указанным индексом."""
        step = self.config.survey[index]

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            message = update.effective_message
            if message is None:
                return index

            if self.rate_limited(context):
                await self.reply(update, context, self.config.rate_limit.message)
                return index

            text = message.text or message.caption or ""
            result = validate(step, text)

            # --- некорректный ввод: остаёмся на том же шаге -------------
            if not result.ok:
                fallback_key = {
                    "yes_no": "invalid_yes_no",
                    "number": "invalid_number",
                    "text": "invalid_text",
                }[step.type]
                variants = step.invalid or self.config.message(fallback_key)
                await self.reply(
                    update, context, self.render(variants, context, step), self.keyboard_for(step)
                )
                if self.config.logging.log_steps:
                    logger.info(
                        "Некорректный ответ на шаге '%s' (%s): %r",
                        step.key, result.reason, text[:100],
                    )
                return index

            # --- ответ принят -------------------------------------------
            context.user_data.setdefault(ANSWERS, {})[step.key] = result.value
            context.user_data.setdefault(RAW, {})[step.key] = result.raw
            if self.config.logging.log_steps:
                logger.info("Шаг '%s' = %r", step.key, result.value)

            # --- вежливое завершение (не заинтересован) ------------------
            if result.stop:
                await self.reply(
                    update,
                    context,
                    self.render(self.config.message("not_interested"), context, step),
                    remove_keyboard(),
                )
                await self.persist(update, context, status="not_interested")
                context.user_data.clear()
                return ConversationHandler.END

            # --- отказ по условиям --------------------------------------
            if result.reject:
                variants = step.reject or self.config.message("not_interested")
                await self.reply(
                    update, context, self.render(variants, context, step), remove_keyboard()
                )
                logger.info(
                    "Кандидат %s отсеян на шаге '%s' (значение: %s)",
                    getattr(update.effective_user, "id", "?"), step.key, result.value,
                )
                await self.persist(update, context, status=f"rejected:{step.key}")
                context.user_data.clear()
                return ConversationHandler.END

            # --- следующий шаг ------------------------------------------
            if index + 1 < len(self.config.survey):
                return await self.ask(update, context, index + 1)
            return await self.finish(update, context)

        handler.__name__ = f"step_{step.key}"
        return handler

    async def non_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Пользователь прислал стикер/фото/голос вместо ответа."""
        await self.reply(
            update,
            context,
            self.render(self.config.message("invalid_text"), context),
        )

    # ------------------------------------------------------------------ #
    #  Финал
    # ------------------------------------------------------------------ #

    def summary_fields(self, answers: dict[str, Any], *, with_suffix: bool = True) -> str:
        """Строки вида «ФИО: Иванов Иван Иванович» по шагам с label."""
        lines = []
        for step in self.config.summary_steps:
            if step.key in answers:
                value = answers[step.key]
                suffix = f" {step.suffix}" if step.suffix and with_suffix else ""
                lines.append(f"{step.label}: {value}{suffix}")
        return "\n".join(lines)

    async def finish(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Сводка + финальное сообщение + уведомление менеджера + выгрузки."""
        answers = dict(context.user_data.get(ANSWERS, {}))
        values = self.context_values(context)
        values["fields"] = self.summary_fields(answers)

        summary = render(self.config.message("summary"), values, emoji=self.config.emoji)
        await self.reply(update, context, summary, remove_keyboard())

        finish_text = self.render(self.config.message("finish"), context)
        await self.reply(update, context, finish_text)

        user = update.effective_user
        logger.info(
            "Кандидат %s (@%s) успешно прошёл опрос",
            getattr(user, "id", "?"), getattr(user, "username", None),
        )

        await self.persist(update, context, status="completed")
        await self.notify_manager(update, context, answers)

        context.user_data.clear()
        return ConversationHandler.END

    async def persist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, status: str
    ) -> None:
        """Запись в SQLite и (для успешных анкет) в Google Sheets."""
        user = update.effective_user
        if user is None:
            return
        answers = dict(context.user_data.get(ANSWERS, {}))
        if not answers:
            return

        created_at = self.now()

        if self.storage is not None:
            await self.storage.save(
                user_id=user.id,
                username=(f"@{user.username}" if user.username else None),
                first_name=user.first_name,
                answers=answers,
                status=status,
                created_at=created_at,
            )

        if self.sheets is not None and self.sheets.enabled and status == "completed":
            await self.sheets.append(
                answers=answers,
                username=(f"@{user.username}" if user.username else "—"),
                user_id=user.id,
                status="Прошёл отбор",
                created_at=created_at,
            )

    async def notify_manager(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, answers: dict[str, Any]
    ) -> None:
        """Шлёт карточку кандидата в чат менеджера. Ошибка не прерывает диалог."""
        chat_id = self.config.manager_chat_id
        if not chat_id:
            logger.warning(
                "manager_chat_id не задан — уведомление о кандидате не отправлено "
                "(укажите MANAGER_CHAT_ID или bot.manager_chat_id в конфиге)"
            )
            return

        user = update.effective_user
        values = dict(self.config.vars)
        values.update(answers)
        values.update(
            {
                "bot_name": self.config.bot_name,
                "fields": self.summary_fields(answers, with_suffix=False),
                "username": (f"@{user.username}" if user and user.username else "—"),
                "user_id": getattr(user, "id", "—"),
                "first_name": getattr(user, "first_name", "") or "",
                "datetime": self.now().strftime("%d.%m.%Y %H:%M"),
            }
        )
        text = render(
            self.config.manager_notification, values, emoji=self.config.emoji
        )

        target: str | int = chat_id
        if isinstance(chat_id, str) and not chat_id.startswith("@"):
            try:
                target = int(chat_id)
            except ValueError:
                target = chat_id

        try:
            await context.bot.send_message(chat_id=target, text=text)
            logger.info("Уведомление о кандидате %s отправлено менеджеру", getattr(user, "id", "?"))
        except TelegramError as exc:
            logger.error("Не удалось отправить уведомление менеджеру (%s): %s", target, exc)

    # ------------------------------------------------------------------ #
    #  Команды
    # ------------------------------------------------------------------ #

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        logger.info(
            "Старт диалога: %s (@%s)",
            getattr(user, "id", "?"), getattr(user, "username", None),
        )
        context.user_data.clear()
        context.user_data[ANSWERS] = {}
        context.user_data[RAW] = {}
        context.user_data[STARTED_AT] = self.now().isoformat()
        return await self.ask(update, context, 0)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        logger.info("Диалог отменён пользователем %s", getattr(update.effective_user, "id", "?"))
        text = self.render(self.config.message("cancel"), context)
        await self.reply(update, context, text, remove_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    async def cmd_idle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сообщение вне диалога."""
        await self.reply(update, context, self.render(self.config.message("idle"), context))

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Количество сохранённых анкет. Доступно только чату менеджера."""
        chat = update.effective_chat
        manager = self.config.manager_chat_id
        if not manager or not chat or str(chat.id) != str(manager):
            return
        total = await self.storage.count() if self.storage else 0
        await self.reply(update, context, f"Всего анкет в базе: {total}")

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Глобальный обработчик исключений — бот не должен падать."""
        logger.exception("Ошибка при обработке обновления", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=render(self.config.message("error"), {}, emoji=self.config.emoji),
                )
            except TelegramError:
                pass

    # ------------------------------------------------------------------ #
    #  Сборка
    # ------------------------------------------------------------------ #

    def build_conversation(self) -> ConversationHandler:
        states = {
            index: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.make_step_handler(index)),
                MessageHandler(
                    ~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                    self.non_text_handler,
                ),
            ]
            for index in range(len(self.config.survey))
        }

        return ConversationHandler(
            entry_points=[CommandHandler("start", self.cmd_start)],
            states=states,
            fallbacks=[
                CommandHandler("cancel", self.cmd_cancel),
                CommandHandler("stop", self.cmd_cancel),
                CommandHandler("start", self.cmd_start),
            ],
            name="candidate_survey",
            allow_reentry=True,
        )

    def register(self, application: Application) -> None:
        """Вешает все обработчики на приложение."""
        application.add_handler(self.build_conversation())
        application.add_handler(CommandHandler("stats", self.cmd_stats))
        application.add_handler(CommandHandler("cancel", self.cmd_cancel))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.cmd_idle)
        )
        application.add_error_handler(self.on_error)
