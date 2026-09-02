"""Клавиатуры."""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

from .config import ButtonsConfig


def yes_no_keyboard(buttons: ButtonsConfig) -> ReplyKeyboardMarkup:
    """Две кнопки «Да» / «Нет» в один ряд."""
    return ReplyKeyboardMarkup(
        [[buttons.yes, buttons.no]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Да или Нет",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """Убирает клавиатуру там, где ждём свободный текст."""
    return ReplyKeyboardRemove()
