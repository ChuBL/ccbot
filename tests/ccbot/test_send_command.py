"""Tests for the /send command handler in bot.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.message.text = text
    return update


@pytest.fixture
def bot_env():
    """Patch what the handler touches; the topic is bound to @5 running in /work."""
    with (
        patch("ccbot.bot.is_user_allowed", return_value=True),
        patch("ccbot.bot._get_thread_id", return_value=42),
        patch("ccbot.bot.session_manager") as sm,
        patch("ccbot.bot.tmux_manager") as tmux,
        patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as reply,
        patch("ccbot.bot.send_file_command", new_callable=AsyncMock) as send,
    ):
        sm.resolve_window_for_thread.return_value = "@5"
        sm.get_display_name.return_value = "project"
        tmux.find_window_by_id = AsyncMock(return_value=MagicMock(cwd="/work"))
        yield sm, tmux, reply, send


class TestSendCommand:
    async def test_passes_the_window_cwd(self, bot_env) -> None:
        from ccbot.bot import send_command

        _, _, _, send = bot_env
        update = _make_update("/send report.pdf")
        await send_command(update, MagicMock())

        send.assert_awaited_once_with(update.message, "/work")

    async def test_unbound_topic_sends_nothing(self, bot_env) -> None:
        from ccbot.bot import send_command

        sm, _, reply, send = bot_env
        sm.resolve_window_for_thread.return_value = None
        await send_command(_make_update("/send report.pdf"), MagicMock())

        send.assert_not_awaited()
        assert "No session bound" in reply.await_args.args[1]

    async def test_user_who_is_not_allowed_is_ignored(self, bot_env) -> None:
        from ccbot.bot import send_command

        _, _, reply, send = bot_env
        with patch("ccbot.bot.is_user_allowed", return_value=False):
            await send_command(_make_update("/send /etc/passwd"), MagicMock())

        send.assert_not_awaited()
        reply.assert_not_awaited()
