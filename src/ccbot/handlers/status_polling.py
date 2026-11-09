"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import html
import logging
import re
import time

from telegram import Bot, ReplyParameters
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import is_interactive_ui, parse_status_line
from ..tmux_manager import tmux_manager
from .cleanup import clear_topic_state
from .interactive_ui import (
    SHELLS,
    announce_ready,
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
    release_held_text,
)
from .message_queue import enqueue_status_update, get_message_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Local patch: a message id no chat has, for _private_topic_deleted
_NO_SUCH_MESSAGE_ID = 2147483000

# Local patch: pass on the "claude --resume" line Claude Code prints when it quits.
# Anchored to the start of a line because the same words can be quoted inside a conversation.
# Claude Code repeats the flags it was launched with ahead of --resume.
_RE_RESUME_LINE = re.compile(
    r"^\s*Resume this session with:[ \t]*\n\s*"
    r"(claude\b[^\n]*--resume\s+[0-9a-fA-F-]{36}[^\n]*?)[ \t]*$",
    re.MULTILINE,
)
# window_id -> resume command already handled ("" while something other than a shell runs)
_resume_seen: dict[str, str] = {}


async def _forward_resume_line(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    pane_command: str,
    pane_text: str,
) -> None:
    """Send the resume command to the topic once per exit."""
    if pane_command not in SHELLS:
        _resume_seen[window_id] = ""
        return
    found = _RE_RESUME_LINE.findall(pane_text)
    command = found[-1] if found else ""
    if window_id not in _resume_seen:
        # First look at this window (e.g. the bot restarted): old text, not a new exit
        _resume_seen[window_id] = command
        return
    if not command or command == _resume_seen[window_id]:
        return
    thread_kwargs = {} if thread_id is None else {"message_thread_id": thread_id}
    await bot.send_message(
        chat_id=session_manager.resolve_chat_id(user_id, thread_id),
        text=f"Resume this session with:\n<code>{html.escape(command)}</code>",
        parse_mode="HTML",
        **thread_kwargs,  # type: ignore[arg-type]
    )
    _resume_seen[window_id] = command


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    await _forward_resume_line(
        bot, user_id, window_id, thread_id, w.pane_current_command, pane_text
    )
    await announce_ready(bot, window_id, w.pane_current_command, pane_text)
    await release_held_text(bot, window_id, w.pane_current_command, pane_text)

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    status_line = parse_status_line(pane_text)

    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def _private_topic_deleted(bot: Bot, chat_id: int, thread_id: int) -> bool:
    """Whether a private-chat topic is gone (local patch).

    unpin_all_forum_topic_messages succeeds there even for a topic that never
    existed. A reply to a message that doesn't exist is always refused, so nothing
    is ever sent, and Telegram checks the topic before it checks the message.
    """
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=".",
            disable_notification=True,
            reply_parameters=ReplyParameters(
                message_id=_NO_SUCH_MESSAGE_ID,
                allow_sending_without_reply=False,
            ),
        )
    except BadRequest as e:
        return "message thread not found" in str(e).lower()
    return False


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for user_id, thread_id, wid in list(
                    session_manager.iter_thread_bindings()
                ):
                    try:
                        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                        if chat_id > 0:
                            # Local patch: a private-chat topic, see _private_topic_deleted.
                            # Only unbinds: a group topic whose chat id was lost resolves
                            # here too and looks deleted, so never kill the window on this.
                            if await _private_topic_deleted(bot, chat_id, thread_id):
                                session_manager.unbind_thread(user_id, thread_id)
                                await clear_topic_state(user_id, thread_id, bot)
                                logger.info(
                                    "Topic deleted: unbound thread %d for user %d, "
                                    "window_id '%s' can be picked again",
                                    thread_id,
                                    user_id,
                                    wid,
                                )
                            continue
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for user_id, thread_id, wid in list(session_manager.iter_thread_bindings()):
                try:
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        session_manager.unbind_thread(user_id, thread_id)
                        await clear_topic_state(user_id, thread_id, bot)
                        logger.info(
                            "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                            user_id,
                            thread_id,
                            wid,
                        )
                        continue

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
