"""Tests for /start in bound and unbound topics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.tmux_manager import TmuxWindow


def _make_update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.user_data = {"_pending_thread_text": "stale"}
    return context


@pytest.fixture
def bot_env():
    with (
        patch("ccbot.bot.is_user_allowed", return_value=True),
        patch("ccbot.bot._get_thread_id", return_value=42),
        patch("ccbot.bot.session_manager") as sm,
        patch("ccbot.bot.tmux_manager") as tmux,
        patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as reply,
    ):
        sm.get_window_for_thread.return_value = None
        sm.iter_thread_bindings.return_value = []
        tmux.list_windows = AsyncMock(return_value=[])
        yield sm, tmux, reply


class TestStartCommand:
    async def test_unbound_topic_lists_unbound_windows(self, bot_env) -> None:
        from ccbot.bot import STATE_KEY, STATE_SELECTING_WINDOW, start_command

        _, tmux, reply = bot_env
        tmux.list_windows.return_value = [TmuxWindow("@1", "app", "/work/app")]
        context = _make_context()
        await start_command(_make_update(), context)
        assert "Bind to Existing Window" in reply.await_args.args[1]
        assert context.user_data[STATE_KEY] == STATE_SELECTING_WINDOW
        assert context.user_data["_pending_thread_id"] == 42
        assert "_pending_thread_text" not in context.user_data

    async def test_unbound_topic_without_windows_browses(self, bot_env) -> None:
        from ccbot.bot import start_command

        _, _, reply = bot_env
        await start_command(_make_update(), _make_context())
        assert "Select Working Directory" in reply.await_args.args[1]

    async def test_bound_topic_only_greets(self, bot_env) -> None:
        from ccbot.bot import start_command

        sm, tmux, reply = bot_env
        sm.get_window_for_thread.return_value = "@1"
        await start_command(_make_update(), _make_context())
        assert "reply_markup" not in reply.await_args.kwargs
        tmux.list_windows.assert_not_awaited()
