"""Tests for forwarding the `claude --resume` line when Claude Code quits."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers import status_polling
from ccbot.handlers.status_polling import _forward_resume_line

SID = "11111111-2222-3333-4444-555555555555"
EXIT_TEXT = f"Resume this session with:\nclaude --resume {SID}\nuser@host $\n"


@pytest.fixture(autouse=True)
def _state():
    status_polling._resume_seen.clear()
    with patch("ccbot.handlers.status_polling.session_manager") as sm:
        sm.resolve_chat_id.return_value = 1
        yield
    status_polling._resume_seen.clear()


async def _tick(bot: AsyncMock, command: str, text: str) -> None:
    await _forward_resume_line(bot, 1, "@1", 7, command, text)


class TestForwardResumeLine:
    async def test_sent_once_per_exit(self) -> None:
        bot = AsyncMock()
        await _tick(bot, "claude", "working")
        await _tick(bot, "zsh", EXIT_TEXT)
        await _tick(bot, "zsh", EXIT_TEXT)
        bot.send_message.assert_awaited_once()
        assert SID in bot.send_message.await_args.kwargs["text"]

    async def test_old_text_after_a_bot_restart_is_not_sent(self) -> None:
        bot = AsyncMock()
        await _tick(bot, "zsh", EXIT_TEXT)
        bot.send_message.assert_not_awaited()

    async def test_quoted_inside_a_conversation_is_ignored(self) -> None:
        bot = AsyncMock()
        await _tick(bot, "claude", "working")
        await _tick(
            bot, "zsh", f"he said Resume this session with:\nclaude --resume {SID} ok?"
        )
        bot.send_message.assert_not_awaited()

    async def test_nothing_while_claude_code_runs(self) -> None:
        bot = AsyncMock()
        await _tick(bot, "claude", "working")
        await _tick(bot, "claude", EXIT_TEXT)
        bot.send_message.assert_not_awaited()
