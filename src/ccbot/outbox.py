"""File outbox — lets a process inside a tmux window ask the bot to send a file.

`ccbot send <file>` runs inside a Claude Code pane, where the bot token is
not available. It drops a request file into <CCBOT_DIR>/outbox/ and waits for
the running bot to deliver the file to the topic bound to that window and
write a result file back.

This module must NOT import config.py (same reason as hook.py): it runs in
panes where the bot env vars are scrubbed.

Key pieces:
  - validate_file(): the checks shared by the CLI and the bot.
  - submit_request() / wait_for_result(): CLI side.
  - claim_requests() / write_result(): bot side.
  - send_main(): `ccbot send` entry point.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .utils import atomic_write_json, ccbot_dir

logger = logging.getLogger(__name__)

# Telegram Bot API upload limit for sendDocument
MAX_FILE_BYTES = 50 * 1024 * 1024

# A request the bot has not claimed within this time means no bot is running
PICKUP_TIMEOUT = 10.0
# Requests older than this are dropped unsent: their CLI has given up already
REQUEST_TTL = 60.0
# Result files nobody collected are removed after this long
RESULT_TTL = 600.0

_REQUEST_SUFFIX = ".json"
_RESULT_SUFFIX = ".result"
_WAIT_INTERVAL = 0.2


@dataclass
class OutboxRequest:
    """One file a tmux window asked the bot to send."""

    request_id: str
    window_key: str  # "tmux_session:@window_id", same form as session_map keys
    path: str
    caption: str = ""


def outbox_dir() -> Path:
    """Directory holding pending requests and their results."""
    return ccbot_dir() / "outbox"


def validate_file(path: Path) -> str | None:
    """Return why `path` cannot be sent as a Telegram document, or None if it can."""
    try:
        if not path.exists():
            return f"File not found: {path}"
        if not path.is_file():
            return f"Not a regular file: {path}"
        size = path.stat().st_size
    except OSError as e:
        return f"Cannot read {path}: {e}"
    if size == 0:
        return f"File is empty: {path}"
    if size > MAX_FILE_BYTES:
        return (
            f"File is too large: {path} is {size / 1024 / 1024:.1f} MB, "
            f"the Telegram limit is {MAX_FILE_BYTES // 1024 // 1024} MB"
        )
    if not os.access(path, os.R_OK):
        return f"Cannot read {path}: permission denied"
    return None


# --- CLI side ---


def current_window_key() -> str | None:
    """Return "session:@window_id" for the tmux pane this process runs in."""
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        return None
    try:
        result = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                pane_id,
                "-p",
                "#{session_name}:#{window_id}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    key = result.stdout.strip()
    session_name, _, window_id = key.rpartition(":")
    if not session_name or not window_id.startswith("@"):
        return None
    return key


def submit_request(window_key: str, path: Path, caption: str = "") -> str:
    """Queue a file for the bot to send. Returns the request id."""
    request_id = uuid.uuid4().hex
    atomic_write_json(
        outbox_dir() / f"{request_id}{_REQUEST_SUFFIX}",
        {"window_key": window_key, "path": str(path), "caption": caption},
    )
    return request_id


def wait_for_result(request_id: str, timeout: float) -> tuple[bool, str]:
    """Wait for the bot to handle a request. Returns (ok, error message).

    The request is withdrawn if the bot never claims it, so a file is not
    sent long after the caller was told it failed.
    """
    request_file = outbox_dir() / f"{request_id}{_REQUEST_SUFFIX}"
    result_file = outbox_dir() / f"{request_id}{_RESULT_SUFFIX}"
    start = time.monotonic()
    while True:
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text())
            except (json.JSONDecodeError, OSError):
                result = {"ok": False, "error": "unreadable result from the bot"}
            result_file.unlink(missing_ok=True)
            return bool(result.get("ok")), str(result.get("error", ""))

        elapsed = time.monotonic() - start
        if elapsed > PICKUP_TIMEOUT and request_file.exists():
            try:
                request_file.unlink()
            except FileNotFoundError:
                continue  # claimed just now: keep waiting for its result
            return False, "the ccbot bot did not pick up the request; is it running?"
        if elapsed > timeout:
            return False, f"no result from the bot after {timeout:.0f}s"
        time.sleep(_WAIT_INTERVAL)


# --- Bot side ---


def claim_requests() -> list[OutboxRequest]:
    """Take every pending request out of the outbox, oldest first.

    A claimed request is deleted before it is returned, so a crash during
    delivery cannot make the bot send the same file again on restart.
    Stale requests and uncollected results are removed along the way.
    """
    directory = outbox_dir()
    if not directory.is_dir():
        return []

    now = time.time()
    pending: list[tuple[float, OutboxRequest]] = []
    for entry in directory.iterdir():
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue  # withdrawn by its CLI between listing and stat
        if entry.suffix == _RESULT_SUFFIX:
            if now - mtime > RESULT_TTL:
                entry.unlink(missing_ok=True)
            continue
        if entry.suffix != _REQUEST_SUFFIX or entry.name.startswith("."):
            continue

        try:
            data = json.loads(entry.read_text())
            entry.unlink()
        except (json.JSONDecodeError, OSError):
            entry.unlink(missing_ok=True)
            continue
        if now - mtime > REQUEST_TTL:
            logger.warning("Dropping stale outbox request %s", entry.name)
            continue
        if (
            not isinstance(data, dict)
            or not data.get("window_key")
            or not data.get("path")
        ):
            write_result(entry.stem, False, "malformed request")
            continue
        pending.append(
            (
                mtime,
                OutboxRequest(
                    request_id=entry.stem,
                    window_key=str(data["window_key"]),
                    path=str(data["path"]),
                    caption=str(data.get("caption", "")),
                ),
            )
        )
    return [request for _, request in sorted(pending, key=lambda item: item[0])]


def write_result(request_id: str, ok: bool, error: str = "") -> None:
    """Report the outcome of a request back to the waiting CLI."""
    atomic_write_json(
        outbox_dir() / f"{request_id}{_RESULT_SUFFIX}", {"ok": ok, "error": error}
    )


# --- `ccbot send` ---


def send_main() -> None:
    """Send files to the Telegram topic bound to the current tmux window."""
    parser = argparse.ArgumentParser(
        prog="ccbot send",
        description="Send files to the Telegram topic bound to this tmux window",
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to send")
    parser.add_argument("--caption", default="", help="Caption shown with each file")
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for the upload of each file (default: 180)",
    )
    args = parser.parse_args(sys.argv[2:])

    window_key = current_window_key()
    if not window_key:
        print(
            "ccbot send: not inside a tmux window (TMUX_PANE is unset or unknown)",
            file=sys.stderr,
        )
        sys.exit(1)

    failed = False
    for file in args.files:
        path = file.expanduser().resolve()
        error = validate_file(path)
        if not error:
            request_id = submit_request(window_key, path, args.caption)
            ok, error = wait_for_result(request_id, args.timeout)
            if ok:
                print(f"Sent {path}")
                continue
        print(f"ccbot send: {error}", file=sys.stderr)
        failed = True
    sys.exit(1 if failed else 0)
