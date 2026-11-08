"""Tests for how the bot treats messages while the trust dialog is up."""

from unittest.mock import AsyncMock, MagicMock, patch

from ccbot.handlers.callback_data import CB_TRUST_NO, CB_TRUST_YES

DIALOG = (
    " Accessing workspace:\n /work\n ❯ No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n"
)


def _context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


async def test_text_is_not_typed_into_the_dialog() -> None:
    from ccbot.bot import text_handler

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.message.text = "hello"
    update.message.chat.send_action = AsyncMock()
    with (
        patch("ccbot.bot.is_user_allowed", return_value=True),
        patch("ccbot.bot._get_thread_id", return_value=42),
        patch("ccbot.bot.session_manager") as sm,
        patch("ccbot.bot.tmux_manager") as tmux,
        patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as reply,
        patch("ccbot.bot.handle_interactive_ui", new_callable=AsyncMock),
        patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
    ):
        sm.get_window_for_thread.return_value = "@5"
        sm.send_to_window = AsyncMock(return_value=(True, "ok"))
        tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        tmux.capture_pane = AsyncMock(return_value=DIALOG)
        await text_handler(update, _context())
    sm.send_to_window.assert_not_awaited()
    assert "trust prompt" in reply.await_args.args[1]


async def _press(data: str, bound: str | None = "@5") -> AsyncMock:
    from ccbot.bot import callback_handler

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.type = "private"
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    with (
        patch("ccbot.bot.is_user_allowed", return_value=True),
        patch("ccbot.bot._get_thread_id", return_value=42),
        patch("ccbot.bot.session_manager") as sm,
        patch("ccbot.bot.clear_interactive_msg", new_callable=AsyncMock),
        patch(
            "ccbot.bot.answer_trust_dialog",
            new_callable=AsyncMock,
            return_value=(True, "done"),
        ) as answer,
    ):
        sm.get_window_for_thread.return_value = bound
        await callback_handler(update, _context())
    return answer


async def test_buttons_answer_the_bound_window() -> None:
    (await _press(f"{CB_TRUST_YES}@5")).assert_awaited_once_with("@5", True)
    (await _press(f"{CB_TRUST_NO}@5")).assert_awaited_once_with("@5", False)


async def test_old_button_for_another_window_does_nothing() -> None:
    (await _press(f"{CB_TRUST_YES}@9")).assert_not_awaited()
