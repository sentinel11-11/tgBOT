"""Тесты работы с текстом: подстановки, вариативность, эмодзи, задержки."""

from __future__ import annotations

from bot.texts import pick, render, safe_format, strip_emoji, typing_delay


def test_safe_format_substitutes():
    assert safe_format("Возраст до {max} лет", {"max": 63}) == "Возраст до 63 лет"


def test_safe_format_keeps_unknown_placeholders():
    assert safe_format("Привет, {name}!", {}) == "Привет, {name}!"


def test_safe_format_survives_broken_template():
    assert "{" in safe_format("100% и { скобка", {})


def test_strip_emoji():
    assert strip_emoji("🔔 Новый кандидат! ✅") == "Новый кандидат!"


def test_render_without_emoji():
    text = render(["Готово 👍"], {}, emoji=False)
    assert text == "Готово"


def test_pick_returns_variant_from_list():
    variants = ["раз", "два", "три"]
    assert pick(variants) in variants


def test_pick_covers_all_variants_over_many_calls():
    variants = ["а", "б", "в"]
    seen = {pick(variants) for _ in range(200)}
    assert seen == set(variants)


def test_pick_empty_returns_fallback():
    assert pick([], "запасной") == "запасной"


def test_typing_delay_respects_bounds():
    for _ in range(50):
        delay = typing_delay("x" * 500, min_delay=1.0, max_delay=2.0, per_char=0.05, cap=5.0)
        assert 0 < delay <= 5.0


def test_typing_delay_grows_with_length():
    short = typing_delay("ок", min_delay=1.0, max_delay=1.0, per_char=0.01, cap=99)
    long = typing_delay("а" * 200, min_delay=1.0, max_delay=1.0, per_char=0.01, cap=99)
    assert long > short
