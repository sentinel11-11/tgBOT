"""Загрузка и валидация конфигурации.

Приоритет источников (от низшего к высшему):
    значения по умолчанию  ->  config.yaml  ->  переменные окружения (.env)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

# --------------------------------------------------------------------------- #
#  Исключения
# --------------------------------------------------------------------------- #


class ConfigError(Exception):
    """Некорректная или неполная конфигурация."""


# --------------------------------------------------------------------------- #
#  Вспомогательное
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "y", "on", "да"}
_FALSE = {"0", "false", "no", "n", "off", "нет"}

VALID_STEP_TYPES = {"yes_no", "text", "number"}
VALID_REJECT_ON = {"yes", "no", "out_of_range", None}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _env_str(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def as_list(value: Any) -> list[str]:
    """Строку превращает в список из одного элемента, список оставляет как есть."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return [str(value)]


# --------------------------------------------------------------------------- #
#  Шаг опроса
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """Один вопрос сценария."""

    key: str
    type: str
    questions: list[str]
    label: str | None = None
    invalid: list[str] = field(default_factory=list)
    reject: list[str] = field(default_factory=list)
    reject_on: str | None = None
    stop_on: str | None = None
    yes_value: str = "Да"
    no_value: str = "Нет"
    suffix: str = ""            # приписка в сводке, например «лет»
    min_length: int = 1
    min: int | None = None
    max: int | None = None

    @property
    def in_summary(self) -> bool:
        """Показывать ли шаг в итоговой сводке и отчётах."""
        return bool(self.label)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "Step":
        if not isinstance(raw, dict):
            raise ConfigError(f"survey[{index}]: ожидался словарь, получено {type(raw).__name__}")

        key = str(raw.get("key") or "").strip()
        if not key:
            raise ConfigError(f"survey[{index}]: не задан обязательный параметр 'key'")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ConfigError(
                f"survey[{index}] ('{key}'): 'key' должен быть латиницей без пробелов "
                "(допустимы буквы, цифры и подчёркивание)"
            )

        step_type = str(raw.get("type") or "text").strip()
        if step_type not in VALID_STEP_TYPES:
            raise ConfigError(
                f"survey '{key}': неизвестный type='{step_type}'. "
                f"Допустимо: {', '.join(sorted(VALID_STEP_TYPES))}"
            )

        questions = as_list(raw.get("questions"))
        if not questions:
            raise ConfigError(f"survey '{key}': не задан ни один вариант вопроса ('questions')")

        reject_on = raw.get("reject_on")
        if reject_on is not None:
            reject_on = str(reject_on).strip().lower()
        if reject_on not in VALID_REJECT_ON:
            raise ConfigError(
                f"survey '{key}': недопустимое reject_on='{reject_on}'. "
                "Допустимо: yes, no, out_of_range"
            )
        if reject_on == "out_of_range" and step_type != "number":
            raise ConfigError(
                f"survey '{key}': reject_on='out_of_range' применим только к type='number'"
            )
        if reject_on in {"yes", "no"} and step_type != "yes_no":
            raise ConfigError(
                f"survey '{key}': reject_on='{reject_on}' применим только к type='yes_no'"
            )

        stop_on = raw.get("stop_on")
        if stop_on is not None:
            stop_on = str(stop_on).strip().lower()
            if stop_on not in {"yes", "no"}:
                raise ConfigError(f"survey '{key}': stop_on может быть только 'yes' или 'no'")

        step = cls(
            key=key,
            type=step_type,
            questions=questions,
            label=(str(raw["label"]) if raw.get("label") else None),
            invalid=as_list(raw.get("invalid")),
            reject=as_list(raw.get("reject")),
            reject_on=reject_on,
            stop_on=stop_on,
            yes_value=str(raw.get("yes_value", "Да")),
            no_value=str(raw.get("no_value", "Нет")),
            suffix=str(raw.get("suffix", "") or ""),
            min_length=int(raw.get("min_length", 1)),
            min=(int(raw["min"]) if raw.get("min") is not None else None),
            max=(int(raw["max"]) if raw.get("max") is not None else None),
        )

        if step.type == "number" and step.min is not None and step.max is not None:
            if step.min > step.max:
                raise ConfigError(f"survey '{key}': min ({step.min}) больше max ({step.max})")

        return step


# --------------------------------------------------------------------------- #
#  Секции конфига
# --------------------------------------------------------------------------- #


@dataclass
class TypingConfig:
    enabled: bool = True
    min_delay: float = 1.2
    max_delay: float = 2.4
    per_char: float = 0.012
    max_delay_cap: float = 5.0


@dataclass
class RateLimitConfig:
    enabled: bool = False
    max_messages: int = 25
    per_seconds: int = 60
    message: str = "Слишком много сообщений подряд. Давайте чуть помедленнее."


@dataclass
class DatabaseConfig:
    enabled: bool = True
    path: str = "data/candidates.db"


@dataclass
class SheetsConfig:
    enabled: bool = False
    credentials_file: str = "credentials.json"
    spreadsheet_id: str = ""
    worksheet: str = "Кандидаты"
    write_header: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "logs/bot.log"
    log_steps: bool = True


@dataclass
class ButtonsConfig:
    yes: str = "Да"
    no: str = "Нет"
    yes_synonyms: list[str] = field(default_factory=list)
    no_synonyms: list[str] = field(default_factory=list)


@dataclass
class Config:
    """Полная конфигурация приложения."""

    token: str
    bot_name: str = "Консультант"
    manager_chat_id: str | None = None
    proxy: str | None = None
    emoji: bool = True
    timezone: str = "Europe/Moscow"

    typing: TypingConfig = field(default_factory=TypingConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    sheets: SheetsConfig = field(default_factory=SheetsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)

    vars: dict[str, Any] = field(default_factory=dict)
    messages: dict[str, list[str]] = field(default_factory=dict)
    survey: list[Step] = field(default_factory=list)
    manager_notification: str = ""

    # ------------------------------------------------------------------ #

    def message(self, key: str) -> list[str]:
        """Варианты общего сообщения по ключу."""
        return self.messages.get(key, [])

    @property
    def summary_steps(self) -> list[Step]:
        return [step for step in self.survey if step.in_summary]


# --------------------------------------------------------------------------- #
#  Значения по умолчанию для общих сообщений
# --------------------------------------------------------------------------- #

DEFAULT_MESSAGES: dict[str, list[str]] = {
    "not_interested": ["Понял вас, не буду отвлекать. Хорошего дня!"],
    "invalid_yes_no": ["Ответьте «Да» или «Нет», пожалуйста."],
    "invalid_number": ["Пожалуйста, введите число."],
    "invalid_text": ["Напишите, пожалуйста, ответ текстом."],
    "cancel": ["Диалог прерван. Если передумаете — напишите /start снова."],
    "summary": ["Итак, резюмируем:\n\n{fields}"],
    "finish": ["Спасибо! Специалист свяжется с вами в ближайшее время."],
    "idle": ["Чтобы начать, отправьте /start."],
    "error": ["Что-то пошло не так. Попробуйте ещё раз через /start."],
}

DEFAULT_NOTIFICATION = (
    "Новый кандидат!\n\n{fields}\nТелеграм: {username}\nID: {user_id}\nДата: {datetime}"
)


# --------------------------------------------------------------------------- #
#  Загрузка
# --------------------------------------------------------------------------- #


def load_config(path: str | Path | None = None) -> Config:
    """Читает YAML-конфиг, накладывает переменные окружения и валидирует результат."""
    config_path = Path(path or os.getenv("CONFIG_PATH") or "config.yaml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path}: ожидался словарь на верхнем уровне")
        raw = loaded
    elif not os.getenv("BOT_TOKEN"):
        raise ConfigError(
            f"Файл конфигурации '{config_path}' не найден и переменная BOT_TOKEN не задана.\n"
            "Скопируйте шаблон:  cp config.example.yaml config.yaml"
        )

    bot_section = raw.get("bot") or {}

    # --- токен -----------------------------------------------------------
    token = _env_str("BOT_TOKEN", bot_section.get("token"))
    if not token:
        raise ConfigError(
            "Не задан токен бота. Укажите его в переменной окружения BOT_TOKEN "
            "или в config.yaml -> bot.token"
        )
    token = str(token).strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        raise ConfigError(
            "Токен бота выглядит некорректно. Ожидается формат '123456789:AA...' от @BotFather"
        )

    # --- менеджер и прокси ----------------------------------------------
    manager_chat_id = _env_str("MANAGER_CHAT_ID", bot_section.get("manager_chat_id"))
    manager_chat_id = str(manager_chat_id).strip() if manager_chat_id else None

    proxy = _env_str("PROXY_URL", bot_section.get("proxy"))
    proxy = str(proxy).strip() if proxy else None
    if proxy and not re.match(r"^(socks5|socks5h|socks4|http|https)://", proxy):
        raise ConfigError(
            f"Некорректный proxy='{proxy}'. Ожидается вид 'socks5://host:port' или 'http://host:port'"
        )

    # --- секции ----------------------------------------------------------
    typing_raw = raw.get("typing") or {}
    typing = TypingConfig(
        enabled=bool(typing_raw.get("enabled", True)),
        min_delay=float(typing_raw.get("min_delay", 1.2)),
        max_delay=float(typing_raw.get("max_delay", 2.4)),
        per_char=float(typing_raw.get("per_char", 0.012)),
        max_delay_cap=float(typing_raw.get("max_delay_cap", 5.0)),
    )
    if typing.min_delay < 0 or typing.max_delay < typing.min_delay:
        raise ConfigError("typing: должно выполняться 0 <= min_delay <= max_delay")

    rl_raw = raw.get("rate_limit") or {}
    rate_limit = RateLimitConfig(
        enabled=bool(rl_raw.get("enabled", False)),
        max_messages=int(rl_raw.get("max_messages", 25)),
        per_seconds=int(rl_raw.get("per_seconds", 60)),
        message=str(rl_raw.get("message", RateLimitConfig.message)),
    )

    db_raw = raw.get("database") or {}
    database = DatabaseConfig(
        enabled=bool(db_raw.get("enabled", True)),
        path=str(db_raw.get("path", "data/candidates.db")),
    )

    gs_raw = raw.get("google_sheets") or {}
    sheets = SheetsConfig(
        enabled=_env_bool("GOOGLE_SHEETS_ENABLED", bool(gs_raw.get("enabled", False))),
        credentials_file=str(
            _env_str("GOOGLE_CREDENTIALS_FILE", gs_raw.get("credentials_file", "credentials.json"))
        ),
        spreadsheet_id=str(
            _env_str("GOOGLE_SPREADSHEET_ID", gs_raw.get("spreadsheet_id", "")) or ""
        ).strip(),
        worksheet=str(_env_str("GOOGLE_WORKSHEET", gs_raw.get("worksheet", "Кандидаты"))),
        write_header=bool(gs_raw.get("write_header", True)),
    )

    log_raw = raw.get("logging") or {}
    log_file = log_raw.get("file", "logs/bot.log")
    logging_cfg = LoggingConfig(
        level=str(_env_str("LOG_LEVEL", log_raw.get("level", "INFO"))).upper(),
        file=(str(log_file) if log_file else None),
        log_steps=bool(log_raw.get("log_steps", True)),
    )

    btn_raw = raw.get("buttons") or {}
    buttons = ButtonsConfig(
        yes=str(btn_raw.get("yes", "Да")),
        no=str(btn_raw.get("no", "Нет")),
        yes_synonyms=[s.lower() for s in as_list(btn_raw.get("yes_synonyms"))],
        no_synonyms=[s.lower() for s in as_list(btn_raw.get("no_synonyms"))],
    )

    # --- сообщения --------------------------------------------------------
    messages = {key: list(value) for key, value in DEFAULT_MESSAGES.items()}
    for key, value in (raw.get("messages") or {}).items():
        variants = as_list(value)
        if variants:
            messages[str(key)] = variants

    # --- сценарий ---------------------------------------------------------
    survey_raw = raw.get("survey")
    if not survey_raw:
        raise ConfigError("В конфиге не описан ни один шаг опроса (секция 'survey')")
    survey = [Step.from_dict(item, i) for i, item in enumerate(survey_raw)]

    keys = [step.key for step in survey]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise ConfigError(f"В сценарии повторяются ключи шагов: {', '.join(sorted(duplicates))}")

    config = Config(
        token=token,
        bot_name=str(bot_section.get("name", "Консультант")),
        manager_chat_id=manager_chat_id,
        proxy=proxy,
        emoji=bool(bot_section.get("emoji", True)),
        timezone=str(bot_section.get("timezone", "Europe/Moscow")),
        typing=typing,
        rate_limit=rate_limit,
        database=database,
        sheets=sheets,
        logging=logging_cfg,
        buttons=buttons,
        vars=dict(raw.get("vars") or {}),
        messages=messages,
        survey=survey,
        manager_notification=str(raw.get("manager_notification") or DEFAULT_NOTIFICATION),
    )

    if config.sheets.enabled and not config.sheets.spreadsheet_id:
        raise ConfigError(
            "google_sheets.enabled = true, но не задан spreadsheet_id "
            "(config.yaml -> google_sheets.spreadsheet_id или GOOGLE_SPREADSHEET_ID)"
        )

    return config
