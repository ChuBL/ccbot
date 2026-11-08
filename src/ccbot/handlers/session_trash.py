"""Session recycle bin: delete, restore and purge Claude Code transcripts.

Deleting moves a transcript to ``$CCBOT_DIR/trash/<project>/``, where it stays
restorable for 30 days. Sessions still open in a window or in the background are
refused, because Claude Code would keep writing to a file that has been moved.

Key components:
  - claude_agents: Sessions Claude Code currently has open
  - trash_session / restore_session: Move a transcript out and back
  - purge_trash: Permanently remove what is past the keep period
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from ..config import config
from ..session import session_manager
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

_TRASH_KEEP_SECONDS = 30 * 86400
_RE_SESSION_ID = re.compile(r"[0-9a-fA-F-]{36}")
_EXPIRED = "This list has expired. Nothing was changed."


async def claude_agents() -> list[dict[str, str]] | None:
    """Every session Claude Code has open; None if listing fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "agents",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return json.loads(stdout)
    except (OSError, TimeoutError, ValueError) as e:
        logger.warning("Could not list Claude Code sessions: %s", e)
        return None


def _move_with_companion(source: Path, target_dir: Path) -> None:
    """Move a transcript and the folder Claude Code keeps beside it, marking both as moved now."""
    target_dir.mkdir(parents=True, exist_ok=True)
    moved = [target_dir / source.name]
    shutil.move(source, moved[0])
    companion = source.with_suffix("")
    if companion.is_dir() and not (target_dir / companion.name).exists():
        shutil.move(companion, target_dir / companion.name)
        moved.append(target_dir / companion.name)
    for path in moved:
        os.utime(path)  # The 30 days count from the delete, not the last activity


async def trash_session(session_id: str, file_path: str) -> tuple[bool, str]:
    """Move a finished session's transcript to the trash; refuses one still open."""
    agents = await claude_agents()
    if agents is None:
        return False, "Could not tell whether it is still open. Nothing was deleted."
    windows = await tmux_manager.list_windows()
    in_window = any(
        (state := session_manager.window_states.get(w.window_id))
        and state.session_id == session_id
        for w in windows
    )
    if in_window or any(a.get("sessionId") == session_id for a in agents):
        return (
            False,
            "It is still open in a window or in the background. Close it first.",
        )

    source = Path(file_path)
    if source.stem != session_id or not source.is_file():
        return False, _EXPIRED
    try:
        await asyncio.to_thread(
            _move_with_companion,
            source,
            config.config_dir / "trash" / source.parent.name,
        )
    except OSError as e:
        logger.error("Could not trash session %s: %s", session_id, e)
        return False, "Could not delete it."
    return True, "Deleted"


async def restore_session(cwd: str, session_id: str) -> tuple[bool, str]:
    """Move a trashed session back so it can be resumed again."""
    if not _RE_SESSION_ID.fullmatch(session_id):
        return False, _EXPIRED
    source = session_manager.trash_dir_for(cwd) / f"{session_id}.jsonl"
    target_dir = config.claude_projects_path / source.parent.name
    if not source.is_file():
        return False, _EXPIRED
    if (target_dir / source.name).exists():
        return False, "A session with this id already exists. Nothing was changed."
    try:
        await asyncio.to_thread(_move_with_companion, source, target_dir)
    except OSError as e:
        logger.error("Could not restore session %s: %s", session_id, e)
        return False, "Could not restore it."
    return True, "Restored"


def purge_trash() -> None:
    """Permanently remove trashed sessions older than the keep period."""
    cutoff = time.time() - _TRASH_KEEP_SECONDS
    for entry in (config.config_dir / "trash").glob("*/*"):
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            logger.info("Purged trashed session %s", entry.name)
        except OSError as e:
            logger.warning("Could not purge %s: %s", entry, e)
