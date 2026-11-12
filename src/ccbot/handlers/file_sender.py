"""File delivery to Telegram topics — /send and the `ccbot send` outbox.

Sends files from the machine as Telegram documents (uncompressed, any type):
  - send_file: upload one file to a chat/topic.
  - send_file_command: the /send <path> command, path relative to the
    window's working directory.
  - process_outbox / outbox_poll_loop: deliver files queued by `ccbot send`
    (see outbox.py) to every topic bound to the requesting tmux window.

Files go straight to the Bot API rather than through the per-user message
queue: they are not merged or edited, and a large upload must not hold up
text messages.
"""

import asyncio
import logging
from pathlib import Path

from telegram import Bot, Message
from telegram.error import TelegramError

from ..config import config
from ..outbox import OutboxRequest, claim_requests, validate_file, write_result
from ..session import session_manager
from .message_sender import safe_reply

logger = logging.getLogger(__name__)

# How often the outbox directory is checked
OUTBOX_POLL_INTERVAL = 1.0  # seconds

# Uploads of large files need far longer than the default request timeouts
UPLOAD_TIMEOUT = 120.0  # seconds

# Telegram's caption length limit
_MAX_CAPTION_LENGTH = 1024

_SEND_USAGE = (
    "Usage: `/send <path>`\n"
    "Sends a file from the machine to this topic. "
    "A relative path starts at the session's working directory."
)


async def send_file(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    path: Path,
    caption: str = "",
) -> str | None:
    """Upload a file as a document. Returns an error message, or None on success."""
    error = validate_file(path)
    if error:
        return error
    try:
        with path.open("rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=path.name,
                caption=caption[:_MAX_CAPTION_LENGTH] or None,
                message_thread_id=thread_id,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
            )
    except (TelegramError, OSError) as e:
        logger.error("Failed to send %s to %d: %s", path, chat_id, e)
        return f"Failed to send {path.name}: {e}"
    logger.info("Sent file %s to chat %d (thread %s)", path, chat_id, thread_id)
    return None


def resolve_send_path(arg: str, cwd: str) -> Path:
    """Turn the argument of /send into a path; relative ones start at cwd."""
    path = Path(arg).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


async def send_file_command(message: Message, cwd: str) -> None:
    """Handle `/send <path>` for a topic whose window runs in `cwd`."""
    parts = (message.text or "").split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        await safe_reply(message, _SEND_USAGE)
        return

    error = await send_file(
        message.get_bot(),
        message.chat_id,
        message.message_thread_id,
        resolve_send_path(arg, cwd),
    )
    if error:
        await safe_reply(message, f"❌ {error}")


# --- Outbox (`ccbot send`) ---


async def _deliver(bot: Bot, request: OutboxRequest) -> str | None:
    """Send an outbox request to the topics bound to its window."""
    tmux_session, _, window_id = request.window_key.rpartition(":")
    if tmux_session != config.tmux_session_name:
        return (
            f"tmux session '{tmux_session}' is not the one the bot manages "
            f"('{config.tmux_session_name}')"
        )

    # Several users can be bound to one window; a shared group topic gets one copy
    targets = {
        (session_manager.resolve_chat_id(user_id, thread_id), thread_id)
        for user_id, thread_id, wid in session_manager.iter_thread_bindings()
        if wid == window_id
    }
    if not targets:
        return f"no Telegram topic is bound to tmux window {window_id}"

    errors = [
        error
        for chat_id, thread_id in sorted(targets)
        if (
            error := await send_file(
                bot, chat_id, thread_id, Path(request.path), request.caption
            )
        )
    ]
    return errors[0] if errors else None


async def process_outbox(bot: Bot) -> None:
    """Deliver every pending `ccbot send` request and report each outcome."""
    for request in claim_requests():
        try:
            error = await _deliver(bot, request)
        except Exception as e:
            logger.exception("Outbox request %s failed", request.request_id)
            error = f"unexpected error: {e}"
        write_result(request.request_id, error is None, error or "")


async def outbox_poll_loop(bot: Bot) -> None:
    """Background task: watch the outbox for files queued by `ccbot send`."""
    logger.info("Outbox polling started (interval: %ss)", OUTBOX_POLL_INTERVAL)
    while True:
        try:
            await process_outbox(bot)
        except Exception:
            logger.exception("Outbox poll error")
        await asyncio.sleep(OUTBOX_POLL_INTERVAL)
