"""Tests for attaching a topic to an existing tmux window."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_WIN_BIND
from ccbot.handlers.directory_browser import build_window_picker
from ccbot.handlers.interactive_ui import send_window_snapshot
from ccbot.terminal_parser import screen_tail
from ccbot.tmux_manager import TmuxWindow

WINDOW = TmuxWindow("@5", "app", "/work/app", "zsh")


def test_picker_shows_what_each_window_runs() -> None:
    text, _, ids = build_window_picker([("@5", "app", "/work/app", "claude")])
    assert "`claude`" in text
    assert ids == ["@5"]


class TestScreenTail:
    def test_drops_blanks_and_keeps_the_end(self) -> None:
        pane = "\n".join(f"line {i}\n" for i in range(50))
        lines = screen_tail(pane, max_lines=3).split("\n")
        assert lines == ["line 47", "line 48", "line 49"]

    def test_fits_a_telegram_message(self) -> None:
        assert len(screen_tail("x" * 9000)) <= 3500


class TestSnapshot:
    async def _send(self, window: TmuxWindow, pane: str) -> str:
        bot = AsyncMock()
        with (
            patch("ccbot.handlers.interactive_ui.session_manager") as sm,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as tmux,
        ):
            sm.resolve_chat_id.return_value = 1
            tmux.find_window_by_id = AsyncMock(return_value=window)
            tmux.capture_pane = AsyncMock(return_value=pane)
            await send_window_snapshot(bot, 1, "@5", 7)
        return bot.send_message.await_args.kwargs["text"]

    async def test_says_when_a_window_is_at_the_shell(self) -> None:
        text = await self._send(WINDOW, "user@host $ a<b\n")
        assert "<b>zsh</b>" in text
        assert "at the shell" in text
        assert "a&lt;b" in text

    async def test_claude_code_window(self) -> None:
        text = await self._send(TmuxWindow("@5", "app", "/w", "claude"), "❯ hi\n")
        assert "<b>claude</b>" in text
        assert "at the shell" not in text

    async def test_empty_screen(self) -> None:
        assert "screen is empty" in await self._send(WINDOW, "\n\n")


class TestFirstMessage:
    async def _bind(self, window: TmuxWindow, pane: str):
        update = MagicMock()
        update.effective_user.id = 1
        update.effective_chat.type = "private"
        update.callback_query.data = f"{CB_WIN_BIND}0"
        update.callback_query.answer = AsyncMock()
        context = MagicMock()
        context.bot = AsyncMock()
        context.user_data = {
            "_pending_thread_id": 42,
            "_pending_thread_text": "hello",
        }
        from ccbot.bot import UNBOUND_WINDOWS_KEY, callback_handler

        context.user_data[UNBOUND_WINDOWS_KEY] = ["@5"]
        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as sm,
            patch("ccbot.bot.tmux_manager") as tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch("ccbot.bot.safe_send", new_callable=AsyncMock) as send,
            patch("ccbot.bot.send_window_snapshot", new_callable=AsyncMock) as snap,
        ):
            sm.send_to_window = AsyncMock(return_value=(True, "ok"))
            tmux.find_window_by_id = AsyncMock(return_value=window)
            tmux.capture_pane = AsyncMock(return_value=pane)
            await callback_handler(update, context)
        return sm, send, snap

    @pytest.mark.parametrize(
        ("command", "pane"),
        [
            ("zsh", "user@host $"),
            ("claude", "Do you want to proceed?\n x\n y\n Esc to cancel\n"),
        ],
    )
    async def test_held_back_from_shell_or_prompt(self, command, pane) -> None:
        sm, send, snap = await self._bind(TmuxWindow("@5", "a", "/w", command), pane)
        sm.send_to_window.assert_not_awaited()
        send.assert_awaited_once()
        snap.assert_awaited_once()

    async def test_forwarded_to_a_waiting_claude_code(self) -> None:
        sm, send, _ = await self._bind(TmuxWindow("@5", "a", "/w", "claude"), "❯ ")
        sm.send_to_window.assert_awaited_once_with("@5", "hello")
        send.assert_not_awaited()
