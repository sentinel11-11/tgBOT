"""Работа с текстом: подстановки, вариативность фраз, эмодзи."""

from __future__ import annotations

import random
import re
from typing import Any, Mapping, Sequence

# Диапазоны эмодзи и пиктограмм (для режима emoji: false).
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "\U00002460-\U000024ff"
    "\U000025a0-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0001f000-\U0001f2ff"
    "\U0000fe0f"
    "\U0000200d"
    "]+",
    flags=re.UNICODE,
)


class _SafeDict(dict):
    """Оставляет неизвестные плейсхолдеры как есть, вместо KeyError."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - тривиально
        return "{" + key + "}"


def safe_format(template: str, values: Mapping[str, Any] | None = None) -> str:
    """Подставляет {переменные}; неизвестные и «кривые» шаблоны не роняют бота."""
    if not template:
        return ""
    if not values:
        values = {}
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        # Например, одиночная фигурная скобка в тексте — отдаём как есть.
        return template


def strip_emoji(text: str) -> str:
    """Удаляет эмодзи и подчищает лишние пробелы."""
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return cleaned.strip()


def pick(variants: Sequence[str] | str | None, fallback: str = "") -> str:
    """Случайный вариант фразы — та самая вариативность из ТЗ."""
    if not variants:
        return fallback
    if isinstance(variants, str):
        return variants
    return random.choice(list(variants))


def render(
    variants: Sequence[str] | str | None,
    values: Mapping[str, Any] | None = None,
    *,
    fallback: str = "",
    emoji: bool = True,
) -> str:
    """Выбирает случайный вариант, подставляет переменные, при необходимости чистит эмодзи."""
    text = safe_format(pick(variants, fallback), values)
    if not emoji:
        text = strip_emoji(text)
    return text


def typing_delay(
    text: str,
    *,
    min_delay: float,
    max_delay: float,
    per_char: float,
    cap: float,
) -> float:
    """Сколько секунд «печатать» текст: случайная база + надбавка за длину."""
    base = random.uniform(min_delay, max_delay) if max_delay > min_delay else min_delay
    delay = base + len(text) * per_char
    return max(0.0, min(delay, cap))
