"""Тесты разбора и валидации ответов."""

from __future__ import annotations

import pytest

from bot.config import ButtonsConfig, Step
from bot.validators import bind_buttons, parse_number, parse_yes_no, validate

BUTTONS = ButtonsConfig(
    yes="Да",
    no="Нет",
    yes_synonyms=["да", "ага", "конечно", "+", "ок", "интересно"],
    no_synonyms=["нет", "неа", "не интересно", "-", "нет проблем"],
)


@pytest.fixture(autouse=True)
def _bind():
    bind_buttons(BUTTONS)


# --------------------------------------------------------------------------- #
#  Да / Нет
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Да", True),
        ("да", True),
        ("ДА!", True),
        ("ага", True),
        ("Конечно", True),
        ("+", True),
        ("Нет", False),
        ("нет.", False),
        ("НЕА", False),
        ("не интересно", False),
        ("нет проблем", False),   # отрицание важнее «проблем»
        ("Может быть", None),
        ("не знаю", None),
        ("", None),
        ("завтра перезвоните", None),
    ],
)
def test_parse_yes_no(text, expected):
    assert parse_yes_no(text, BUTTONS) is expected


# --------------------------------------------------------------------------- #
#  Числа
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("35", 35),
        (" 42 ", 42),
        ("мне 30 лет", 30),
        ("35 лет", 35),
        ("около 30", None),        # неточный ответ — переспрашиваем (п.8 ТЗ)
        ("чуть за сорок", None),
        ("где-то 25", None),
        ("тридцать пять", 35),
        ("сорок", 40),
        ("много", None),
        ("", None),
        ("30-40", None),
        ("абв", None),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


# --------------------------------------------------------------------------- #
#  Валидация шагов
# --------------------------------------------------------------------------- #

def make_step(**kwargs) -> Step:
    raw = {"key": "test", "questions": ["?"]}
    raw.update(kwargs)
    return Step.from_dict(raw, 0)


def test_text_step_requires_min_length():
    step = make_step(type="text", label="ФИО", min_length=3)
    assert validate(step, "Ив").ok is False
    assert validate(step, "Ив").reason == "invalid"

    result = validate(step, "Иванов Иван Иванович")
    assert result.ok and result.value == "Иванов Иван Иванович"


def test_number_step_in_range():
    step = make_step(type="number", label="Возраст", min=1, max=63, reject_on="out_of_range")
    ok = validate(step, "35")
    assert ok.ok and ok.value == 35 and not ok.reject


def test_number_step_out_of_range_rejects():
    step = make_step(type="number", label="Возраст", min=1, max=63, reject_on="out_of_range")
    result = validate(step, "70")
    assert result.ok and result.reject is True and result.value == 70


def test_number_step_out_of_range_without_reject_asks_again():
    step = make_step(type="number", label="Возраст", min=18, max=63)
    result = validate(step, "5")
    assert result.ok is False and result.reason == "out_of_range"


def test_yes_no_reject_on_yes():
    step = make_step(
        type="yes_no", label="Здоровье", reject_on="yes",
        yes_value="Есть ограничения", no_value="Нет проблем",
    )
    bad = validate(step, "да")
    assert bad.ok and bad.reject and bad.value == "Есть ограничения"

    good = validate(step, "нет")
    assert good.ok and not good.reject and good.value == "Нет проблем"


def test_yes_no_stop_on_no():
    step = make_step(type="yes_no", stop_on="no")
    result = validate(step, "нет")
    assert result.ok and result.stop and not result.reject


def test_yes_no_unrecognized():
    step = make_step(type="yes_no")
    result = validate(step, "может быть")
    assert result.ok is False and result.reason == "invalid"
