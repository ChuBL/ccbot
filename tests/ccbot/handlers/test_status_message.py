"""Tests for the status message when Telegram requests time out.

A request that times out may still have gone through, so the status message
must not be sent again on every poll.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, TimedOut

from ccbot.handlers import message_queue


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=999)
    return bot


class TestStatusMessageDuringTelegramOutage:
    """A request that times out may still have gone through."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        message_queue._status_msg_info.clear()
        message_queue._status_send_retry_at.clear()
        with patch("ccbot.handlers.message_queue.session_manager") as sm:
            sm.resolve_chat_id.return_value = 1
            yield
        message_queue._status_msg_info.clear()
        message_queue._status_send_retry_at.clear()

    @staticmethod
    def _task(text: str) -> message_queue.MessageTask:
        return message_queue.MessageTask(
            task_type="status_update", text=text, window_id="@1", thread_id=7
        )

    async def test_an_edit_that_times_out_keeps_the_message(self, mock_bot) -> None:
        message_queue._status_msg_info[(1, 7)] = (99, "@1", "`ctx 8%`")
        mock_bot.edit_message_text.side_effect = TimedOut()

        await message_queue._process_status_update_task(
            mock_bot, 1, self._task("`ctx 9%`")
        )

        mock_bot.send_message.assert_not_awaited()
        mock_bot.edit_message_text.assert_awaited_once()
        # The old text stays recorded, so the next poll edits again.
        assert message_queue._status_msg_info[(1, 7)] == (99, "@1", "`ctx 8%`")

    async def test_an_edit_of_a_deleted_message_still_sends_anew(
        self, mock_bot
    ) -> None:
        message_queue._status_msg_info[(1, 7)] = (99, "@1", "`ctx 8%`")
        mock_bot.edit_message_text.side_effect = BadRequest("Message to edit not found")

        await message_queue._process_status_update_task(
            mock_bot, 1, self._task("`ctx 9%`")
        )

        mock_bot.send_message.assert_awaited_once()
        assert message_queue._status_msg_info[(1, 7)] == (999, "@1", "`ctx 9%`")

    async def test_a_send_that_times_out_is_not_repeated_right_away(
        self, mock_bot
    ) -> None:
        mock_bot.send_message.side_effect = TimedOut()

        for _ in range(3):
            await message_queue._process_status_update_task(
                mock_bot, 1, self._task("`ctx 9%`")
            )

        # One attempt: no plain text resend, and no new message on the next polls.
        mock_bot.send_message.assert_awaited_once()

    async def test_sending_resumes_after_the_wait(self, mock_bot) -> None:
        mock_bot.send_message.side_effect = TimedOut()
        await message_queue._process_status_update_task(
            mock_bot, 1, self._task("`ctx 9%`")
        )
        mock_bot.send_message.side_effect = None

        with patch.object(
            message_queue.time,
            "monotonic",
            return_value=message_queue._status_send_retry_at[(1, 7)] + 1,
        ):
            await message_queue._process_status_update_task(
                mock_bot, 1, self._task("`ctx 9%`")
            )

        assert message_queue._status_msg_info[(1, 7)] == (999, "@1", "`ctx 9%`")
        assert not message_queue._status_send_retry_at

    async def test_a_format_error_still_falls_back_to_plain_text(
        self, mock_bot
    ) -> None:
        sent = MagicMock(message_id=999)
        mock_bot.send_message.side_effect = [BadRequest("can't parse entities"), sent]

        await message_queue._process_status_update_task(
            mock_bot, 1, self._task("`ctx 9%`")
        )

        assert mock_bot.send_message.await_count == 2
        assert message_queue._status_msg_info[(1, 7)][0] == 999
