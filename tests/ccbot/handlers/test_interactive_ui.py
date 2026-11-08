"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
    list_background_sessions,
    open_background_session,
)
from ccbot.handlers.callback_data import (
    CB_AGENT_OPEN,
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)


class TestAgentViewButtons:
    SESSIONS: ClassVar[list[dict[str, str]]] = [
        {"id": "abc12345", "sessionId": "sid", "name": "fix tests", "cwd": "/work"}
    ]
    AGENT_VIEW_PANE = "  New sessions: x\n  ● a\n  ctrl+x to delete\n"

    def test_one_button_per_session_above_the_keys(self) -> None:
        keyboard = _build_interactive_keyboard("@5", "AgentView", self.SESSIONS)
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "▶ fix tests"
        assert button.callback_data == f"{CB_AGENT_OPEN}abc12345:@5"
        assert len(keyboard.inline_keyboard) == 4  # one session + three rows of keys

    async def test_lists_only_background_sessions(self) -> None:
        agents = b'[{"kind": "background", "id": "a", "sessionId": "s"},'
        agents += b' {"kind": "interactive", "id": null, "sessionId": "t"}]'
        proc = MagicMock(communicate=AsyncMock(return_value=(agents, b"")))
        with patch(
            "ccbot.handlers.interactive_ui.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            sessions = await list_background_sessions()
        assert [s["id"] for s in sessions] == ["a"]

    async def test_old_button_does_not_exit_a_conversation_in_use(self) -> None:
        tmux = MagicMock()
        tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        tmux.capture_pane = AsyncMock(return_value="❯ an ordinary conversation")
        tmux.send_keys = AsyncMock()
        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager", tmux),
            patch(
                "ccbot.handlers.interactive_ui.list_background_sessions",
                AsyncMock(return_value=self.SESSIONS),
            ),
        ):
            ok, _ = await open_background_session("@5", "abc12345")
        assert not ok
        tmux.send_keys.assert_not_awaited()

    async def test_exits_then_attaches_then_points_the_window(self) -> None:
        tmux = MagicMock()
        tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        tmux.capture_pane = AsyncMock(return_value=self.AGENT_VIEW_PANE)
        tmux.send_keys = AsyncMock(return_value=True)
        point = AsyncMock(return_value=True)
        with (
            patch("ccbot.handlers.interactive_ui.tmux_manager", tmux),
            patch(
                "ccbot.handlers.interactive_ui.list_background_sessions",
                AsyncMock(return_value=self.SESSIONS),
            ),
            patch(
                "ccbot.handlers.interactive_ui._wait_for_pane",
                AsyncMock(return_value=True),
            ),
            patch("ccbot.handlers.interactive_ui._point_window_at_session", point),
        ):
            ok, _ = await open_background_session("@5", "abc12345")
        assert ok
        assert [c.args[1] for c in tmux.send_keys.call_args_list] == [
            "/exit",
            "claude attach abc12345",
        ]
        point.assert_awaited_once_with("@5", "sid", "/work")
