"""Тесты загрузки и валидации конфигурации."""

from __future__ import annotations

import pytest
import yaml

from bot.config import ConfigError, load_config
from tests.conftest import VALID_TOKEN


def write_config(tmp_path, data) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


MINIMAL = {
    "bot": {"token": VALID_TOKEN, "name": "Григорий"},
    "survey": [{"key": "interest", "type": "yes_no", "questions": ["Интересно?"], "stop_on": "no"}],
}


def test_example_config_loads(example_config_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    cfg = load_config(example_config_path)

    assert cfg.bot_name == "Григорий"
    assert [step.key for step in cfg.survey] == [
        "interest", "full_name", "phone", "age", "age_comment",
        "health", "health_details", "crime", "crime_article", "crime_comment",
    ]
    assert [label for _, label in cfg.report_fields] == [
        "ФИО", "Телефон", "Возраст", "Здоровье", "Судимости", "Статья", "Комментарий",
    ]

    age = cfg.survey[3]
    assert age.type == "number" and age.max == 63
    assert age.reject_on is None, "кандидатов по возрасту больше не отсеиваем"
    assert age.accept_out_of_range is True

    # Уточняющие вопросы задаются только при соответствующих ответах
    assert cfg.survey[4].ask_if == {"step": "age", "out_of_range": True}
    assert cfg.survey[6].ask_if == {"step": "health", "equals": "yes"}
    assert cfg.survey[8].ask_if == {"step": "crime", "equals": "yes"}
    assert not any(step.reject_on for step in cfg.survey), "отсева в сценарии нет"
    assert len(cfg.message("invalid_yes_no")) >= 3   # вариативность фраз


def test_env_overrides_yaml(tmp_path, monkeypatch):
    path = write_config(tmp_path, {**MINIMAL, "bot": {**MINIMAL["bot"], "proxy": None}})
    monkeypatch.setenv("BOT_TOKEN", "111111111:BBoverriddenTokenFromEnv_0987654321xy")
    monkeypatch.setenv("MANAGER_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("PROXY_URL", "socks5://127.0.0.1:1081")

    cfg = load_config(path)
    assert cfg.token.startswith("111111111:")
    assert cfg.manager_chat_id == "-1001234567890"
    assert cfg.proxy == "socks5://127.0.0.1:1081"


def test_missing_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    data = {"survey": MINIMAL["survey"]}
    with pytest.raises(ConfigError, match="токен"):
        load_config(write_config(tmp_path, data))


def test_malformed_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    data = {**MINIMAL, "bot": {"token": "not-a-token"}}
    with pytest.raises(ConfigError, match="Токен"):
        load_config(write_config(tmp_path, data))


def test_bad_proxy_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PROXY_URL", raising=False)
    data = {**MINIMAL, "bot": {**MINIMAL["bot"], "proxy": "127.0.0.1:1081"}}
    with pytest.raises(ConfigError, match="proxy"):
        load_config(write_config(tmp_path, data))


def test_empty_survey_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="survey"):
        load_config(write_config(tmp_path, {"bot": {"token": VALID_TOKEN}, "survey": []}))


def test_duplicate_keys_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    data = {
        "bot": {"token": VALID_TOKEN},
        "survey": [
            {"key": "age", "type": "number", "questions": ["?"]},
            {"key": "age", "type": "number", "questions": ["?"]},
        ],
    }
    with pytest.raises(ConfigError, match="повторя"):
        load_config(write_config(tmp_path, data))


def test_unknown_step_type_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    data = {"bot": {"token": VALID_TOKEN}, "survey": [{"key": "x", "type": "date", "questions": ["?"]}]}
    with pytest.raises(ConfigError, match="type"):
        load_config(write_config(tmp_path, data))


def test_sheets_enabled_without_id_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_SPREADSHEET_ID", raising=False)
    data = {**MINIMAL, "google_sheets": {"enabled": True, "spreadsheet_id": ""}}
    with pytest.raises(ConfigError, match="spreadsheet_id"):
        load_config(write_config(tmp_path, data))


def test_new_step_can_be_added_from_config_only(tmp_path, monkeypatch):
    """Добавление шага в YAML не требует изменений в коде."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    data = {
        "bot": {"token": VALID_TOKEN},
        "survey": MINIMAL["survey"] + [
            {"key": "license", "type": "yes_no", "label": "Права кат. C", "questions": ["Права есть?"]},
        ],
    }
    cfg = load_config(write_config(tmp_path, data))
    assert len(cfg.survey) == 2
    assert cfg.survey[1].label == "Права кат. C"


# --------------------------------------------------------------------------- #
#  Условные вопросы и объединённые колонки
# --------------------------------------------------------------------------- #

def test_should_ask_depends_on_previous_answer(example_config_path, monkeypatch):
    """Уточнение про здоровье задаётся только при ответе «Да»."""
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    cfg = load_config(example_config_path)
    details = next(step for step in cfg.survey if step.key == "health_details")

    assert cfg.should_ask(details, {"health": "Есть ограничения"}) is True
    assert cfg.should_ask(details, {"health": "Нет проблем"}) is False
    assert cfg.should_ask(details, {}) is False, "без ответа вопрос не задаём"


def test_should_ask_on_out_of_range(example_config_path, monkeypatch):
    """Уточнение про возраст — только если он вне допустимого диапазона."""
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    cfg = load_config(example_config_path)
    comment = next(step for step in cfg.survey if step.key == "age_comment")

    assert cfg.should_ask(comment, {"age": 70}) is True
    assert cfg.should_ask(comment, {"age": 35}) is False
    assert cfg.should_ask(comment, {"age": "не помню"}) is False


def test_report_value_joins_and_falls_back_to_dash(example_config_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    cfg = load_config(example_config_path)

    assert cfg.report_value("comment", {"health_details": "астма"}) == "Здоровье: астма"
    assert cfg.report_value("comment", {"health_details": "—"}) == "—", "прочерк не склеиваем"
    assert cfg.report_value("comment", {}) == "—"
    assert cfg.report_value("age", {"age": 70}) == "70"


def test_bad_ask_if_is_rejected(tmp_path, monkeypatch):
    """Опечатка в условии обнаруживается при загрузке конфига."""
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    path = tmp_path / "config.yaml"
    path.write_text(
        "survey:\n"
        "  - key: a\n    type: yes_no\n    questions: ['Да?']\n"
        "  - key: b\n    type: text\n    questions: ['Что?']\n"
        "    ask_if: {step: a}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ask_if"):
        load_config(str(path))
