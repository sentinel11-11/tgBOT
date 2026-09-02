"""Тесты хранилища и подготовки строк для Google Sheets."""

from __future__ import annotations

import sqlite3

import pytest

from bot.sheets import SheetsExporter
from bot.storage import CandidateStorage

# asyncio_mode=auto (pytest.ini): async-тесты подхватываются автоматически

ANSWERS = {
    "interest": "Да",
    "full_name": "Иванов Иван Иванович",
    "age": 35,
    "health": "Нет проблем",
    "crime": "Нет судимостей",
}


# --------------------------------------------------------------------------- #
#  SQLite
# --------------------------------------------------------------------------- #

async def test_storage_saves_candidate(config, tmp_path):
    storage = CandidateStorage(str(tmp_path / "db.sqlite"), config.survey)
    storage.init()

    row_id = await storage.save(
        user_id=42, username="@ivanov", first_name="Иван", answers=ANSWERS, status="completed"
    )
    assert row_id == 1
    assert await storage.count() == 1

    with sqlite3.connect(tmp_path / "db.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM candidates").fetchone()

    assert row["full_name"] == "Иванов Иван Иванович"
    assert row["age"] == "35"
    assert row["health"] == "Нет проблем"
    assert row["crime"] == "Нет судимостей"
    assert row["username"] == "@ivanov"
    assert row["status"] == "completed"
    assert row["created_at"]


async def test_storage_adds_column_for_new_step(config, tmp_path):
    """Новый шаг в конфиге -> новая колонка, старые данные на месте."""
    db_path = str(tmp_path / "db.sqlite")
    storage = CandidateStorage(db_path, config.survey)
    storage.init()
    await storage.save(user_id=1, username=None, first_name=None, answers=ANSWERS)

    from bot.config import Step

    extended = list(config.survey) + [
        Step.from_dict({"key": "license", "type": "yes_no", "label": "Права", "questions": ["?"]}, 9)
    ]
    storage2 = CandidateStorage(db_path, extended)
    storage2.init()
    await storage2.save(
        user_id=2, username=None, first_name=None, answers={**ANSWERS, "license": "Есть"}
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()

    assert len(rows) == 2
    assert rows[0]["license"] is None
    assert rows[1]["license"] == "Есть"


async def test_storage_disabled_is_noop(config, tmp_path):
    storage = CandidateStorage(str(tmp_path / "none.db"), config.survey, enabled=False)
    storage.init()
    assert await storage.save(user_id=1, username=None, first_name=None, answers=ANSWERS) is None
    assert await storage.count() == 0


# --------------------------------------------------------------------------- #
#  Google Sheets
# --------------------------------------------------------------------------- #

def test_sheets_header_follows_config(config):
    exporter = SheetsExporter(config.sheets, config.survey)
    assert exporter.header() == [
        "Дата и время", "ФИО", "Возраст", "Здоровье", "Судимости",
        "Телеграм", "User ID", "Статус",
    ]


def test_sheets_row_matches_header(config):
    from datetime import datetime

    exporter = SheetsExporter(config.sheets, config.survey)
    row = exporter.row(
        answers=ANSWERS,
        username="@ivanov",
        user_id=42,
        status="Прошёл отбор",
        created_at=datetime(2026, 9, 2, 15, 30, 45),
    )
    assert len(row) == len(exporter.header())
    assert row == [
        "02.09.2026 15:30:45", "Иванов Иван Иванович", "35",
        "Нет проблем", "Нет судимостей", "@ivanov", "42", "Прошёл отбор",
    ]


async def test_sheets_disabled_returns_false(config):
    config.sheets.enabled = False
    exporter = SheetsExporter(config.sheets, config.survey)
    assert await exporter.append(answers=ANSWERS, username="@ivanov", user_id=42) is False


async def test_sheets_error_is_swallowed(config, monkeypatch, caplog):
    """Падение Google Sheets не должно ронять диалог."""
    config.sheets.enabled = True
    config.sheets.spreadsheet_id = "fake"
    exporter = SheetsExporter(config.sheets, config.survey)

    def boom(row):
        raise RuntimeError("API quota exceeded")

    monkeypatch.setattr(exporter, "_append_sync", boom)

    result = await exporter.append(answers=ANSWERS, username="@ivanov", user_id=42)
    assert result is False
    assert "Google Sheets" in caplog.text


# --------------------------------------------------------------------------- #
#  Ошибки Google API: временные повторяем, постоянные объясняем
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status_code): self.status_code = status_code


class FakeAPIError(Exception):
    """Имитация gspread.exceptions.APIError."""
    def __init__(self, status, message=""):
        super().__init__(f"APIError: [{status}]: {message}")
        self.response = FakeResponse(status)


def test_error_status_from_response_and_text():
    from bot.sheets import error_status

    assert error_status(FakeAPIError(503)) == 503
    assert error_status(Exception("APIError: [403]: forbidden")) == 403
    assert error_status(Exception("совсем другая ошибка")) is None


@pytest.mark.parametrize("status,transient", [(503, True), (429, True), (500, True),
                                              (403, False), (404, False)])
def test_transient_classification(status, transient):
    from bot.sheets import is_transient

    assert is_transient(FakeAPIError(status)) is transient


def test_explain_503_does_not_blame_permissions():
    """503 — сбой Google, а не права доступа (реальный случай при настройке)."""
    from bot.sheets import explain

    message = explain(FakeAPIError(503, "The service is currently unavailable."))
    assert "временный сбой" in message.lower()
    assert "доступ" not in message.lower()


def test_explain_403_tells_how_to_fix():
    from bot.sheets import explain

    message = explain(FakeAPIError(403), account_email="bot@project.iam.gserviceaccount.com")
    assert "bot@project.iam.gserviceaccount.com" in message
    assert "Редактор" in message


def test_explain_404_mentions_spreadsheet_id():
    from bot.sheets import explain

    assert "GOOGLE_SPREADSHEET_ID" in explain(FakeAPIError(404), spreadsheet_id="abc123")


async def test_append_retries_transient_error(config, monkeypatch):
    """Первые две попытки — 503, третья успешна: строка должна записаться."""
    config.sheets.enabled = True
    config.sheets.spreadsheet_id = "fake"
    exporter = SheetsExporter(config.sheets, config.survey)
    exporter.retry_delay = 0  # без пауз в тесте

    calls = []

    def flaky(row):
        calls.append(row)
        if len(calls) < 3:
            raise FakeAPIError(503, "The service is currently unavailable.")

    monkeypatch.setattr(exporter, "_append_once", flaky)

    assert await exporter.append(answers=ANSWERS, username="@ivanov", user_id=42) is True
    assert len(calls) == 3


async def test_append_does_not_retry_permission_error(config, monkeypatch):
    """403 повторять бессмысленно — сразу понятное сообщение."""
    config.sheets.enabled = True
    config.sheets.spreadsheet_id = "fake"
    exporter = SheetsExporter(config.sheets, config.survey)
    exporter.retry_delay = 0

    calls = []

    def forbidden(row):
        calls.append(row)
        raise FakeAPIError(403, "The caller does not have permission")

    monkeypatch.setattr(exporter, "_append_once", forbidden)

    assert await exporter.append(answers=ANSWERS, username="@ivanov", user_id=42) is False
    assert len(calls) == 1
    assert "Редактор" in exporter.last_error
