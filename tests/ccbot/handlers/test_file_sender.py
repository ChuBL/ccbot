"""Tests for file delivery: /send and the `ccbot send` outbox."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import TelegramError

from ccbot import outbox
from ccbot.handlers import file_sender


@pytest.fixture(autouse=True)
def ccbot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ccbot-home"
    monkeypatch.setenv("CCBOT_DIR", str(home))
    return home


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7")
    return path


@pytest.fixture
def session_manager():
    """Thread bindings as (user_id, thread_id, window_id); chat id = -100 for groups."""
    with patch("ccbot.handlers.file_sender.session_manager") as sm:
        sm.iter_thread_bindings.return_value = [(1, 42, "@5")]
        sm.resolve_chat_id.side_effect = lambda user_id, thread_id: user_id
        yield sm


@pytest.fixture
def tmux_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_sender.config, "tmux_session_name", "ccbot")


def _make_message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.chat_id = 100
    message.message_thread_id = 42
    message.get_bot.return_value = AsyncMock()
    return message


def _result(request_id: str) -> dict:
    return json.loads((outbox.outbox_dir() / f"{request_id}.result").read_text())


class TestSendFile:
    async def test_uploads_the_file_as_a_document(self, sample_file: Path) -> None:
        bot = AsyncMock()

        assert await file_sender.send_file(bot, 100, 42, sample_file, "hi") is None

        kwargs = bot.send_document.await_args.kwargs
        assert kwargs["chat_id"] == 100
        assert kwargs["message_thread_id"] == 42
        assert kwargs["filename"] == "report.pdf"
        assert kwargs["caption"] == "hi"

    async def test_invalid_file_is_not_uploaded(self, tmp_path: Path) -> None:
        bot = AsyncMock()

        error = await file_sender.send_file(bot, 100, 42, tmp_path / "missing")

        assert "not found" in (error or "")
        bot.send_document.assert_not_awaited()

    async def test_telegram_error_is_returned(self, sample_file: Path) -> None:
        bot = AsyncMock()
        bot.send_document.side_effect = TelegramError("Request Entity Too Large")

        error = await file_sender.send_file(bot, 100, 42, sample_file)

        assert "Request Entity Too Large" in (error or "")


class TestSendFileCommand:
    async def test_without_a_path_replies_with_usage(self) -> None:
        message = _make_message("/send")
        with patch(
            "ccbot.handlers.file_sender.safe_reply", new_callable=AsyncMock
        ) as reply:
            await file_sender.send_file_command(message, "/tmp")

        assert "Usage" in reply.await_args.args[1]
        message.get_bot.return_value.send_document.assert_not_awaited()

    async def test_relative_path_starts_at_the_window_cwd(
        self, sample_file: Path
    ) -> None:
        message = _make_message("/send@my_bot report.pdf")

        await file_sender.send_file_command(message, str(sample_file.parent))

        kwargs = message.get_bot.return_value.send_document.await_args.kwargs
        assert kwargs["document"].name == str(sample_file)
        assert kwargs["chat_id"] == 100
        assert kwargs["message_thread_id"] == 42

    async def test_path_with_spaces(self, tmp_path: Path) -> None:
        path = tmp_path / "my notes.txt"
        path.write_text("notes")
        message = _make_message(f"/send {path}")

        await file_sender.send_file_command(message, "/somewhere/else")

        kwargs = message.get_bot.return_value.send_document.await_args.kwargs
        assert kwargs["filename"] == "my notes.txt"

    async def test_error_is_reported_in_the_topic(self, tmp_path: Path) -> None:
        message = _make_message("/send missing.txt")
        with patch(
            "ccbot.handlers.file_sender.safe_reply", new_callable=AsyncMock
        ) as reply:
            await file_sender.send_file_command(message, str(tmp_path))

        assert "not found" in reply.await_args.args[1]


@pytest.mark.usefixtures("tmux_session")
class TestProcessOutbox:
    async def test_sends_to_the_topic_bound_to_the_window(
        self, sample_file: Path, session_manager: MagicMock
    ) -> None:
        bot = AsyncMock()
        request_id = outbox.submit_request("ccbot:@5", sample_file, "done")

        await file_sender.process_outbox(bot)

        kwargs = bot.send_document.await_args.kwargs
        assert (kwargs["chat_id"], kwargs["message_thread_id"]) == (1, 42)
        assert kwargs["caption"] == "done"
        assert _result(request_id) == {"ok": True, "error": ""}

    async def test_unbound_window_is_an_error(
        self, sample_file: Path, session_manager: MagicMock
    ) -> None:
        bot = AsyncMock()
        request_id = outbox.submit_request("ccbot:@9", sample_file)

        await file_sender.process_outbox(bot)

        bot.send_document.assert_not_awaited()
        assert "no Telegram topic" in _result(request_id)["error"]

    async def test_window_of_another_tmux_session_is_an_error(
        self, sample_file: Path, session_manager: MagicMock
    ) -> None:
        bot = AsyncMock()
        request_id = outbox.submit_request("other:@5", sample_file)

        await file_sender.process_outbox(bot)

        bot.send_document.assert_not_awaited()
        assert not _result(request_id)["ok"]

    async def test_group_topic_shared_by_two_users_gets_one_copy(
        self, sample_file: Path, session_manager: MagicMock
    ) -> None:
        session_manager.iter_thread_bindings.return_value = [
            (1, 42, "@5"),
            (2, 42, "@5"),
        ]
        session_manager.resolve_chat_id.side_effect = lambda user_id, thread_id: -100
        bot = AsyncMock()
        outbox.submit_request("ccbot:@5", sample_file)

        await file_sender.process_outbox(bot)

        bot.send_document.assert_awaited_once()

    async def test_upload_failure_reaches_the_cli(
        self, sample_file: Path, session_manager: MagicMock
    ) -> None:
        bot = AsyncMock()
        bot.send_document.side_effect = TelegramError("Timed out")
        request_id = outbox.submit_request("ccbot:@5", sample_file)

        await file_sender.process_outbox(bot)

        result = _result(request_id)
        assert not result["ok"]
        assert "Timed out" in result["error"]
