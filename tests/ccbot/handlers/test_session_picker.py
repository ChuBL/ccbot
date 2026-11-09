"""Tests for the session picker's button for short old sessions."""

from ccbot.handlers.callback_data import CB_SESSION_ALL
from ccbot.handlers.directory_browser import build_session_picker
from ccbot.session import ClaudeSession

SESSIONS = [
    ClaudeSession("11111111-2222-3333-4444-555555555555", "work", 3, "/nope.jsonl")
]


def _buttons(keyboard) -> list:
    return [b for row in keyboard.inline_keyboard for b in row]


def test_no_hidden_sessions_means_no_extra_button() -> None:
    _, keyboard = build_session_picker(SESSIONS)
    assert [b.text for b in _buttons(keyboard)[-2:]] == ["➕ New Session", "Cancel"]


def test_button_offers_the_hidden_count() -> None:
    _, keyboard = build_session_picker(SESSIONS, hidden=3)
    last = _buttons(keyboard)[-1]
    assert (last.text, last.callback_data) == ("🗂 Show 3 more", CB_SESSION_ALL)
