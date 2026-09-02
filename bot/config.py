"""Загрузка и валидация конфигурации.

Приоритет источников (от низшего к высшему):
    значения по умолчанию  ->  config.yaml  ->  переменные окружения (.env)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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

VALID_STEP_TYPES = {"yes_no", "text", "number", "phone"}
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
    #: Условие «задавать ли вопрос»: {"step": "health", "equals": "yes"}
    #: или {"step": "age", "out_of_range": true}. Пусто — спрашивать всегда.
    ask_if: dict[str, Any] | None = None
    #: Что записать, если вопрос пропущен по условию
    skip_value: str = "—"
    #: Собрать ответ в общую колонку отчёта (например, «Комментарий»)
    merge_into: str | None = None
    #: Принимать значение вне диапазона min/max (вместо повторного вопроса)
    accept_out_of_range: bool = False

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

        ask_if = raw.get("ask_if")
        if ask_if is not None:
            if not isinstance(ask_if, dict) or not str(ask_if.get("step") or "").strip():
                raise ConfigError(
                    f"survey '{key}': ask_if должен быть словарём с ключом 'step', "
                    'например: ask_if: {step: health, equals: "yes"}'
                )
            if "equals" not in ask_if and "out_of_range" not in ask_if:
                raise ConfigError(
                    f"survey '{key}': в ask_if нужно указать equals: yes|no "
                    "или out_of_range: true"
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
            ask_if=ask_if,
            skip_value=str(raw.get("skip_value", "—")),
            accept_out_of_range=bool(raw.get("accept_out_of_range", False)),
            merge_into=(str(raw["merge_into"]).strip() if raw.get("merge_into") else None),
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


# --------------------------------------------------------------------------- #
#  Колонки отчёта: шаги + объединённые колонки (например, «Комментарий»)
# --------------------------------------------------------------------------- #


def report_fields(survey: Sequence["Step"], columns: Mapping[str, str]) -> list[tuple[str, str]]:
    """Пары (ключ, заголовок) в порядке колонок отчёта.

    Обычные шаги с label идут по порядку сценария, а объединённые колонки
    (merge_into) — следом, в порядке первого упоминания.
    """
    fields: list[tuple[str, str]] = []
    merged: list[str] = []
    for step in survey:
        if step.merge_into:
            if step.merge_into not in merged:
                merged.append(step.merge_into)
        elif step.label:
            fields.append((step.key, step.label))
    for key in merged:
        fields.append((key, str(columns.get(key, key))))
    return fields


def report_value(
    survey: Sequence["Step"], key: str, answers: Mapping[str, Any], empty: str = "—"
) -> str:
    """Значение колонки: обычной или собранной из нескольких шагов."""
    parts: list[str] = []
    for step in survey:
        if step.merge_into != key:
            continue
        value = str(answers.get(step.key, "") or "").strip()
        if value and value != step.skip_value:
            parts.append(f"{step.label}: {value}" if step.label else value)
    if parts:
        return "; ".join(parts)

    if any(step.merge_into == key for step in survey):
        return empty

    value = answers.get(key, "")
    return "" if value is None else str(value)


def should_ask(survey: Sequence["Step"], step: "Step", answers: Mapping[str, Any]) -> bool:
    """Задавать ли вопрос: учитывает условие ask_if."""
    condition = step.ask_if
    if not condition:
        return True

    ref_key = str(condition.get("step") or "")
    reference = next((item for item in survey if item.key == ref_key), None)
    if reference is None or ref_key not in answers:
        return False

    value = answers[ref_key]

    if "equals" in condition:
        expected = str(condition["equals"]).strip().lower()
        if reference.type == "yes_no":
            actual = "yes" if str(value) == reference.yes_value else "no"
            return actual == expected
        return str(value).strip().lower() == expected

    if condition.get("out_of_range"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        below = reference.min is not None and number < reference.min
        above = reference.max is not None and number > reference.max
        return bool(below or above)

    return True


@dataclass
class Config:
    """Полная конфигурация приложения."""

    token: str
    bot_name: str = "Консультант"
    #: Куда слать карточки кандидатов. Можно перечислить несколько получателей
    #: через запятую: "123456789, -1001234567890, @channel_name".
    manager_chat_id: str | None = None
    proxy: str | None = None
    emoji: bool = True
    timezone: str = "Europe/Moscow"

    #: Заголовки объединённых колонок отчёта: {"comment": "Комментарий"}
    columns: dict[str, str] = field(default_factory=dict)

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

    @property
    def report_fields(self) -> list[tuple[str, str]]:
        """Колонки отчёта: подписи шагов плюс объединённые колонки."""
        return report_fields(self.survey, self.columns)

    def report_value(self, key: str, answers: Mapping[str, Any]) -> str:
        """Значение колонки отчёта по её ключу."""
        return report_value(self.survey, key, answers)

    def should_ask(self, step: Step, answers: Mapping[str, Any]) -> bool:
        """Нужно ли задавать этот вопрос при текущих ответах."""
        return should_ask(self.survey, step, answers)

    @property
    def manager_chat_ids(self) -> list[str]:
        """Список получателей карточек: MANAGER_CHAT_ID может содержать несколько
        адресатов через запятую (личный чат, группа, канал)."""
        raw = self.manager_chat_id or ""
        return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]

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
    "invalid_phone": ["Не похоже на номер телефона. Например: +7 999 123-45-67."],
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
    else:
        # Без файла сценария бот бесполезен: раньше это выглядело как загадочное
        # «не описан ни один шаг опроса», хотя причина — отсутствующий файл.
        raise ConfigError(
            f"Файл конфигурации '{config_path.resolve()}' не найден.\n"
            "Создайте его одной из команд:\n"
            "    python tools/configure.py        (спросит токен и настройки)\n"
            "    cp config.example.yaml config.yaml"
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
        columns={str(k): str(v) for k, v in (raw.get("columns") or {}).items()},
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
            "google_sheets.enabled = true, но не задан spreadsheet_id.\n"
            "Укажите его в GOOGLE_SPREADSHEET_ID (.env) или config.yaml -> "
            "google_sheets.spreadsheet_id.\n"
            "Если таблицы ещё нет:  python tools/create_sheet.py --share ваша@почта.com"
        )

    return config
