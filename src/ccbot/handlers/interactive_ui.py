"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import asyncio
import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import extract_interactive_content, is_interactive_ui
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_TRUST_NO,
    CB_TRUST_YES,
)
from .message_sender import NO_LINK_PREVIEW

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


SHELLS = frozenset({"zsh", "bash", "sh", "dash", "fish"})
TRUST_DIALOG = "TrustDialog"
_TRUST_MOVE_TRIES = 15
# window_id -> (user_id, thread_id, text): first message waiting for the dialog to be answered
_held_text: dict[str, tuple[int, int | None, str]] = {}
_READY_TIMEOUT_SECONDS = 15.0


def hold_text(window_id: str, user_id: int, thread_id: int | None, text: str) -> None:
    """Keep a message back until the prompt on screen has been answered."""
    _held_text[window_id] = (user_id, thread_id, text)


async def release_held_text(
    bot: Bot, window_id: str, pane_command: str, pane_text: str
) -> None:
    """Send a held message once Claude Code is up, or say it was dropped."""
    held = _held_text.get(window_id)
    if not held or is_interactive_ui(pane_text):
        return
    user_id, thread_id, text = held
    if pane_command in SHELLS:
        del _held_text[window_id]
        await bot.send_message(
            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
            message_thread_id=thread_id,
            text="⚠️ Claude Code is not running, so your first message was not sent.",
        )
    elif session_manager.get_window_state(window_id).session_id:
        del _held_text[window_id]
        await session_manager.send_to_window(window_id, text)


# window_id -> (user_id, thread_id, session id before the trust answer, give-up time)
_awaiting_ready: dict[str, tuple[int, int | None, str, float]] = {}


def expect_ready(window_id: str, user_id: int, thread_id: int | None) -> None:
    """Announce this window once Claude Code has finished starting."""
    _awaiting_ready[window_id] = (
        user_id,
        thread_id,
        session_manager.get_window_state(window_id).session_id,
        time.monotonic() + _READY_TIMEOUT_SECONDS,
    )


async def announce_ready(
    bot: Bot, window_id: str, pane_command: str, pane_text: str
) -> None:
    """Tell the topic Claude Code is up, as its start screen is not forwarded."""
    waiting = _awaiting_ready.get(window_id)
    if not waiting or is_interactive_ui(pane_text):
        return
    user_id, thread_id, earlier, give_up_at = waiting
    if pane_command in SHELLS:
        del _awaiting_ready[window_id]
        return
    # Compared with the earlier id because window ids get reused, and a resumed
    # session already has its id: those start without a new id ever appearing
    current = session_manager.get_window_state(window_id).session_id
    if (current and current != earlier) or time.monotonic() >= give_up_at:
        del _awaiting_ready[window_id]
        await bot.send_message(
            chat_id=session_manager.resolve_chat_id(user_id, thread_id),
            message_thread_id=thread_id,
            text="✅ Claude Code is ready. Send messages here.",
        )


async def answer_trust_dialog(window_id: str, trust: bool) -> tuple[bool, str]:
    """Answer the trust dialog, checking first that it is still on screen."""

    async def on_screen() -> list[str] | None:
        pane_text = await tmux_manager.capture_pane(window_id)
        content = extract_interactive_content(pane_text) if pane_text else None
        if content and content.name == TRUST_DIALOG:
            return content.content.split("\n")
        return None

    lines = await on_screen()
    if lines is None:
        return False, "This prompt has expired. Nothing was changed."
    if trust:
        # Keys sent right after the dialog draws can be lost, and Enter on the
        # default would quit: press it only with the cursor seen on the Yes line
        for _ in range(_TRUST_MOVE_TRIES):
            lines = await on_screen() or []
            cursor = next((i for i, l in enumerate(lines) if l.startswith("❯")), None)
            target = next((i for i, l in enumerate(lines) if "Yes, I trust" in l), None)
            if cursor is None or target is None:
                return False, "Could not read the prompt. Nothing was changed."
            if cursor == target:
                break
            key = "Down" if target > cursor else "Up"
            await tmux_manager.send_keys(window_id, key, enter=False, literal=False)
            await asyncio.sleep(0.3)
        else:
            return False, "Could not move to Yes. Nothing was changed."
        await tmux_manager.send_keys(window_id, "Enter", enter=False, literal=False)
    else:
        await tmux_manager.send_keys(window_id, "Escape", enter=False, literal=False)
    for _ in range(20):
        await asyncio.sleep(0.25)
        if await on_screen() is None:
            if trust:
                return True, "✅ Folder trusted. Claude Code is starting."
            return True, (
                "❌ Claude Code closed. This window is at the shell: "
                "send claude to start it again, or /kill to close the window."
            )
    return False, "Claude Code did not respond to that."


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.
    """
    if ui_name == TRUST_DIALOG:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, trust", callback_data=f"{CB_TRUST_YES}{window_id}"[:64]
                    ),
                    InlineKeyboardButton(
                        "❌ No, exit", callback_data=f"{CB_TRUST_NO}{window_id}"[:64]
                    ),
                ]
            ]
        )

    vertical_only = ui_name == "RestoreCheckpoint"

    rows: list[list[InlineKeyboardButton]] = []
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.
    """
    ikey = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    # Quick check if it looks like an interactive UI
    if not is_interactive_ui(pane_text):
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_text.strip().split("\n")[-3:],
        )
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text)
    if not content:
        return False

    # Build message with navigation keyboard
    keyboard = _build_interactive_keyboard(window_id, ui_name=content.name)

    # Send as plain text (no markdown conversion)
    text = content.content

    # Build thread kwargs for send_message
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    # Check if we have an existing interactive message to edit
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _interactive_mode[ikey] = window_id
            return True
        except BadRequest as e:
            if "Message is not modified" in str(e):
                # Content unchanged — keep existing message as-is
                _interactive_mode[ikey] = window_id
                return True
            # Other edit failure — fall through to send new message,
            # but keep old message until replacement succeeds
            logger.debug(
                "Edit failed for interactive msg %s: %s, sending new",
                existing_msg_id,
                e,
            )
        except Exception as e:
            logger.debug(
                "Edit failed for interactive msg %s: %s, sending new",
                existing_msg_id,
                e,
            )

    # Send new message (plain text — terminal content is not markdown)
    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.error("Failed to send interactive UI: %s", e)
        return False
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_mode[ikey] = window_id
        # New message sent successfully — now safe to delete the old one
        if existing_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=existing_msg_id)
            except Exception:
                pass  # Old message may already be gone
        return True
    return False


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old
