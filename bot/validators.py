"""Разбор и валидация пользовательского ввода."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import ButtonsConfig, Step

# Числа словами — люди часто пишут «тридцать пять» или «около 30».
_WORD_NUMBERS: dict[str, int] = {
    "ноль": 0, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100,
}

# Мусор, который люди дописывают к ФИО.
_NAME_ALLOWED_RE = re.compile(r"^[А-Яа-яЁёA-Za-z\s\.\-']+$")

# Слова, которые допустимо встретить рядом с числом: «мне 35 лет», «35 полных лет».
_NUMBER_FILLERS = {
    "мне", "мне", "уже", "лет", "год", "года", "годик", "годика", "годиков",
    "полных", "полный", "полные", "исполнилось", "стукнуло", "будет", "возраст",
    "я", "сейчас", "ровно", "и", "мой",
}
# Слова-неопределённости: с ними ответ считается неточным и переспрашивается.
_VAGUE_WORDS = {
    "около", "примерно", "приблизительно", "почти", "за", "под", "где", "то",
    "чуть", "больше", "меньше", "свыше", "наверное", "кажется", "плюс", "минус",
    "лишним", "хвостиком", "может", "либо", "или", "думаю", "вроде", "типа",
}


def normalize(text: str) -> str:
    """Приводит ответ к сравнимому виду: нижний регистр, без пунктуации по краям."""
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е")).strip(" .!,;:?)(\"'")


def parse_yes_no(text: str, buttons: ButtonsConfig) -> bool | None:
    """True — «да», False — «нет», None — не распознано."""
    value = normalize(text)
    if not value:
        return None

    yes_words = {normalize(buttons.yes)} | {normalize(s) for s in buttons.yes_synonyms}
    no_words = {normalize(buttons.no)} | {normalize(s) for s in buttons.no_synonyms}
    yes_words.discard("")
    no_words.discard("")

    # Точное совпадение — самый надёжный случай.
    if value in no_words:
        return False
    if value in yes_words:
        return True

    # Отрицание проверяем первым: «нет проблем» не должно стать «да».
    tokens = set(re.findall(r"[\w\+\-]+", value))
    if tokens & no_words:
        return False
    if tokens & yes_words:
        return True

    return None


def parse_number(text: str) -> int | None:
    """Достаёт целое число из ответа.

    Понимает «35», «мне 35 лет», «тридцать пять».
    Не принимает неточные ответы («около 30», «за сорок») и диапазоны («30-40») —
    в этих случаях бот переспрашивает.
    """
    value = normalize(text)
    if not value:
        return None

    words = re.findall(r"[а-яa-z]+", value)
    if any(word in _VAGUE_WORDS for word in words):
        return None

    digits = re.findall(r"\d+", value)
    if len(digits) > 1:
        # «35-36», «30 40» — неоднозначно, просим уточнить.
        return None

    if len(digits) == 1:
        # Рядом с числом допускаем только «служебные» слова.
        if any(word not in _NUMBER_FILLERS for word in words):
            return None
        try:
            return int(digits[0])
        except ValueError:
            return None

    number_words = [word for word in words if word in _WORD_NUMBERS]
    if not number_words:
        return None
    if any(word not in _NUMBER_FILLERS and word not in _WORD_NUMBERS for word in words):
        return None
    if len(number_words) == 1:
        return _WORD_NUMBERS[number_words[0]]
    if len(number_words) == 2:
        tens, ones = _WORD_NUMBERS[number_words[0]], _WORD_NUMBERS[number_words[1]]
        if tens >= 20 and tens % 10 == 0 and 1 <= ones <= 9:
            return tens + ones
    return None


def looks_like_name(text: str) -> bool:
    """Грубая проверка, что в ответе действительно имя, а не набор символов."""
    return bool(_NAME_ALLOWED_RE.fullmatch(text.strip()))


@dataclass
class ValidationResult:
    """Итог проверки ответа на конкретном шаге."""

    ok: bool
    value: Any = None            # значение для сохранения (уже человекочитаемое)
    raw: Any = None              # «сырое» значение (bool / int / str)
    reason: str | None = None    # invalid | out_of_range
    reject: bool = False         # кандидат отсеян
    stop: bool = False           # вежливое завершение (не отказ)


def validate(step: Step, text: str) -> ValidationResult:
    """Проверяет ответ пользователя по правилам шага из конфига."""
    text = (text or "").strip()

    # ---------------- Да / Нет ----------------
    if step.type == "yes_no":
        answer = parse_yes_no(text, validate.buttons)  # type: ignore[attr-defined]
        if answer is None:
            return ValidationResult(ok=False, reason="invalid")

        value = step.yes_value if answer else step.no_value
        answer_key = "yes" if answer else "no"

        if step.stop_on == answer_key:
            return ValidationResult(ok=True, value=value, raw=answer, stop=True)
        if step.reject_on == answer_key:
            return ValidationResult(ok=True, value=value, raw=answer, reject=True)
        return ValidationResult(ok=True, value=value, raw=answer)

    # ---------------- Число ----------------
    if step.type == "number":
        number = parse_number(text)
        if number is None:
            return ValidationResult(ok=False, reason="invalid")

        below = step.min is not None and number < step.min
        above = step.max is not None and number > step.max
        if below or above:
            if step.reject_on == "out_of_range":
                return ValidationResult(ok=True, value=number, raw=number, reject=True)
            return ValidationResult(ok=False, reason="out_of_range")
        return ValidationResult(ok=True, value=number, raw=number)

    # ---------------- Текст ----------------
    if len(text) < max(1, step.min_length):
        return ValidationResult(ok=False, reason="invalid")
    if step.min_length >= 3 and not looks_like_name(text) and not any(c.isalpha() for c in text):
        return ValidationResult(ok=False, reason="invalid")
    return ValidationResult(ok=True, value=text, raw=text)


def bind_buttons(buttons: ButtonsConfig) -> None:
    """Передаёт настройки кнопок в валидатор (вызывается один раз при старте)."""
    validate.buttons = buttons  # type: ignore[attr-defined]


# Значение по умолчанию, чтобы validate() работал и без явной привязки (в тестах).
bind_buttons(ButtonsConfig(yes_synonyms=["да"], no_synonyms=["нет"]))
