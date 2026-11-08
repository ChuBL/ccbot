"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    _held_text,
    answer_trust_dialog,
    handle_interactive_ui,
    hold_text,
    release_held_text,
)
from ccbot.handlers.callback_data import (
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


class TestAnswerTrustDialog:
    FRAME = (
        " Accessing workspace:\n /work\n"
        " {no} No, exit\n {yes} Yes, I trust this folder\n"
        " Enter to confirm · Esc to cancel\n"
    )

    def _tmux(self, *frames: str) -> MagicMock:
        tmux = MagicMock()
        tmux.capture_pane = AsyncMock(side_effect=list(frames))
        tmux.send_keys = AsyncMock(return_value=True)
        return tmux

    def _keys(self, tmux: MagicMock) -> list[str]:
        return [call.args[1] for call in tmux.send_keys.call_args_list]

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        with patch("ccbot.handlers.interactive_ui.asyncio.sleep", AsyncMock()):
            yield

    async def test_enter_only_after_cursor_is_seen_on_yes(self) -> None:
        on_no = self.FRAME.format(no="❯", yes=" ")
        on_yes = self.FRAME.format(no=" ", yes="❯")
        # The first Down is lost: the cursor is still on No when read again
        tmux = self._tmux(on_no, on_no, on_no, on_yes, "started")
        with patch("ccbot.handlers.interactive_ui.tmux_manager", tmux):
            ok, _ = await answer_trust_dialog("@1", True)
        assert ok
        assert self._keys(tmux) == ["Down", "Down", "Enter"]

    async def test_no_presses_escape(self) -> None:
        tmux = self._tmux(self.FRAME.format(no="❯", yes=" "), "user@host ➜")
        with patch("ccbot.handlers.interactive_ui.tmux_manager", tmux):
            ok, _ = await answer_trust_dialog("@1", False)
        assert ok
        assert self._keys(tmux) == ["Escape"]

    async def test_expired_prompt_sends_nothing(self) -> None:
        tmux = self._tmux("user@host ➜")
        with patch("ccbot.handlers.interactive_ui.tmux_manager", tmux):
            ok, _ = await answer_trust_dialog("@1", True)
        assert not ok
        assert self._keys(tmux) == []


class TestHeldText:
    @pytest.fixture(autouse=True)
    def _held(self):
        _held_text.clear()
        hold_text("@1", 1, 7, "hello")
        yield
        _held_text.clear()

    async def test_waits_while_a_prompt_is_up(self, mock_bot) -> None:
        pane = " Accessing workspace:\n x\n y\n Enter to confirm · Esc to cancel\n"
        await release_held_text(mock_bot, "@1", "claude", pane)
        assert "@1" in _held_text

    async def test_dropped_with_a_notice_at_the_shell(self, mock_bot) -> None:
        with patch("ccbot.handlers.interactive_ui.session_manager") as sm:
            sm.resolve_chat_id.return_value = 1
            sm.send_to_window = AsyncMock()
            await release_held_text(mock_bot, "@1", "zsh", "user@host ➜")
        mock_bot.send_message.assert_awaited_once()
        sm.send_to_window.assert_not_awaited()
        assert "@1" not in _held_text

    async def test_sent_once_claude_code_is_up(self, mock_bot) -> None:
        with patch("ccbot.handlers.interactive_ui.session_manager") as sm:
            sm.get_window_state.return_value.session_id = "sid"
            sm.send_to_window = AsyncMock()
            await release_held_text(mock_bot, "@1", "claude", "❯ ")
        sm.send_to_window.assert_awaited_once_with("@1", "hello")
        assert "@1" not in _held_text
