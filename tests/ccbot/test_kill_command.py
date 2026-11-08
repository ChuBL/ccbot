"""Tests for /kill and its confirmation buttons."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_KILL_NO, CB_KILL_YES


def _make_callback_update(data: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


@pytest.fixture
def bot_env():
    """Patch what the callback handler touches; the topic is bound to @5."""
    with (
        patch("ccbot.bot.is_user_allowed", return_value=True),
        patch("ccbot.bot._get_thread_id", return_value=42),
        patch("ccbot.bot.session_manager") as sm,
        patch("ccbot.bot.tmux_manager") as tmux,
        patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as edit,
        patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock),
    ):
        sm.get_window_for_thread.return_value = "@5"
        sm.get_display_name.return_value = "project"
        tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        tmux.kill_window = AsyncMock(return_value=True)
        yield sm, tmux, edit


class TestKillConfirmation:
    async def test_yes_kills_the_bound_window(self, bot_env) -> None:
        from ccbot.bot import callback_handler

        sm, tmux, _ = bot_env
        await callback_handler(
            _make_callback_update(f"{CB_KILL_YES}@5"), _make_context()
        )
        tmux.kill_window.assert_awaited_once_with("@5")
        sm.unbind_thread.assert_called_once_with(1, 42)

    async def test_old_button_for_another_window_kills_nothing(self, bot_env) -> None:
        from ccbot.bot import callback_handler

        sm, tmux, _ = bot_env
        await callback_handler(
            _make_callback_update(f"{CB_KILL_YES}@9"), _make_context()
        )
        tmux.kill_window.assert_not_awaited()
        sm.unbind_thread.assert_not_called()

    async def test_cancel_kills_nothing(self, bot_env) -> None:
        from ccbot.bot import callback_handler

        _, tmux, edit = bot_env
        await callback_handler(_make_callback_update(CB_KILL_NO), _make_context())
        tmux.kill_window.assert_not_awaited()
        edit.assert_awaited_once()


class TestKillCommand:
    async def test_asks_before_killing(self, bot_env) -> None:
        from ccbot.bot import kill_command

        _, tmux, _ = bot_env
        update = MagicMock()
        update.effective_user.id = 1
        with patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as reply:
            await kill_command(update, _make_context())
        reply.assert_awaited_once()
        assert reply.await_args.kwargs["reply_markup"] is not None
        tmux.kill_window.assert_not_awaited()
