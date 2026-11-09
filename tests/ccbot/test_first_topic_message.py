"""The first text in an unbound topic opens the picker and goes no further."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ccbot import bot as bot_mod


def _update(text: str = "name of my topic") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.message_thread_id = 7
    update.message.is_topic_message = True
    return update


async def _send_first_text(windows: list, user_data: dict) -> tuple:
    context = MagicMock(user_data=user_data, bot=AsyncMock())
    refresh = AsyncMock()
    with (
        patch.object(bot_mod, "is_user_allowed", return_value=True),
        patch.object(bot_mod, "_get_thread_id", return_value=7),
        patch.object(bot_mod, "session_manager") as sm,
        patch.object(bot_mod, "tmux_manager") as tm,
        patch.object(bot_mod, "safe_reply", new=AsyncMock()) as reply,
        patch.object(bot_mod, "unbind_deleted_private_topics", new=refresh),
    ):
        sm.get_window_for_thread.return_value = None
        sm.iter_thread_bindings.return_value = []
        sm.send_to_window = AsyncMock()
        tm.list_windows = AsyncMock(return_value=windows)
        await bot_mod.text_handler(_update(), context)
    refresh.assert_awaited_once_with(context.bot)
    return sm, reply


async def test_opens_the_window_picker_and_keeps_nothing_to_send() -> None:
    window = SimpleNamespace(
        window_id="@1", window_name="w", cwd="/x", pane_current_command="zsh"
    )
    user_data: dict = {}
    sm, reply = await _send_first_text([window], user_data)
    reply.assert_awaited_once()
    assert user_data[bot_mod.STATE_KEY] == bot_mod.STATE_SELECTING_WINDOW
    assert user_data["_pending_thread_id"] == 7
    assert not any("text" in key for key in user_data)
    sm.send_to_window.assert_not_awaited()


async def test_opens_the_directory_browser_when_no_window_is_free() -> None:
    user_data: dict = {}
    sm, reply = await _send_first_text([], user_data)
    reply.assert_awaited_once()
    assert user_data[bot_mod.STATE_KEY] == bot_mod.STATE_BROWSING_DIRECTORY
    sm.send_to_window.assert_not_awaited()
