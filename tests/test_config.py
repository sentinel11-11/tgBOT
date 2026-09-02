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
    assert [step.key for step in cfg.survey] == ["interest", "full_name", "age", "health", "crime"]
    assert [step.label for step in cfg.summary_steps] == ["ФИО", "Возраст", "Здоровье", "Судимости"]

    age = cfg.survey[2]
    assert age.type == "number" and age.max == 63 and age.reject_on == "out_of_range"
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
