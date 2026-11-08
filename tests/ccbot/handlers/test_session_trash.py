"""Tests for session_trash — delete, restore, purge and the picker around them."""

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import session_trash
from ccbot.handlers.callback_data import CB_SESSION_DEL, CB_SESSION_RESTORE
from ccbot.handlers.directory_browser import (
    PICKER_DELETE,
    PICKER_TRASH,
    build_session_picker,
)
from ccbot.session import ClaudeSession
from ccbot.tmux_manager import TmuxWindow

SID = "11111111-2222-3333-4444-555555555555"
CWD = "/work/app"
PROJECT = "-work-app"
SESSIONS = [ClaudeSession(SID, "hello", 3, "/nonexistent.jsonl")]


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A projects dir and a trash dir, with no sessions open."""
    projects = tmp_path / "projects"
    (projects / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(session_trash.config, "config_dir", tmp_path)
    monkeypatch.setattr(session_trash.config, "claude_projects_path", projects)
    monkeypatch.setattr(session_trash, "claude_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        session_trash.tmux_manager, "list_windows", AsyncMock(return_value=[])
    )
    return projects / PROJECT, tmp_path / "trash" / PROJECT


def _transcript(project: Path) -> Path:
    path = project / f"{SID}.jsonl"
    path.write_text("{}\n")
    (project / SID).mkdir()
    (project / SID / "tool-output").write_text("x")
    return path


class TestTrashAndRestore:
    async def test_round_trip_keeps_companion_folder(self, dirs) -> None:
        project, trash = dirs
        path = _transcript(project)

        assert await session_trash.trash_session(SID, str(path)) == (True, "Deleted")
        assert not path.exists() and not (project / SID).exists()
        assert (trash / f"{SID}.jsonl").is_file()
        assert (trash / SID / "tool-output").is_file()

        assert await session_trash.restore_session(CWD, SID) == (True, "Restored")
        assert path.is_file() and (project / SID / "tool-output").is_file()
        assert not (trash / f"{SID}.jsonl").exists()

    async def test_refuses_session_open_in_background(self, dirs, monkeypatch) -> None:
        path = _transcript(dirs[0])
        monkeypatch.setattr(
            session_trash,
            "claude_agents",
            AsyncMock(return_value=[{"sessionId": SID}]),
        )
        ok, _ = await session_trash.trash_session(SID, str(path))
        assert not ok and path.exists()

    async def test_refuses_session_open_in_window(self, dirs, monkeypatch) -> None:
        path = _transcript(dirs[0])
        monkeypatch.setattr(
            session_trash.tmux_manager,
            "list_windows",
            AsyncMock(return_value=[TmuxWindow("@1", "w", CWD)]),
        )
        session_trash.session_manager.get_window_state("@1").session_id = SID
        try:
            ok, _ = await session_trash.trash_session(SID, str(path))
        finally:
            session_trash.session_manager.window_states.pop("@1")
        assert not ok and path.exists()

    async def test_refuses_when_open_sessions_are_unknown(
        self, dirs, monkeypatch
    ) -> None:
        path = _transcript(dirs[0])
        monkeypatch.setattr(
            session_trash, "claude_agents", AsyncMock(return_value=None)
        )
        ok, _ = await session_trash.trash_session(SID, str(path))
        assert not ok and path.exists()

    async def test_restore_rejects_path_in_id(self, dirs) -> None:
        ok, _ = await session_trash.restore_session(CWD, "../../etc/passwd")
        assert not ok

    async def test_restore_does_not_overwrite(self, dirs) -> None:
        project, trash = dirs
        trash.mkdir(parents=True)
        (trash / f"{SID}.jsonl").write_text("old\n")
        (project / f"{SID}.jsonl").write_text("new\n")
        ok, _ = await session_trash.restore_session(CWD, SID)
        assert not ok
        assert (project / f"{SID}.jsonl").read_text() == "new\n"


class TestPurge:
    def _aged(self, path: Path, days: float) -> None:
        moment = time.time() - days * 86400
        os.utime(path, (moment, moment))

    async def test_counts_from_delete_not_last_activity(self, dirs) -> None:
        project, trash = dirs
        path = _transcript(project)
        self._aged(path, 90)
        await session_trash.trash_session(SID, str(path))
        session_trash.purge_trash()
        assert (trash / f"{SID}.jsonl").exists()

    def test_removes_only_what_is_past_30_days(self, dirs) -> None:
        trash = dirs[1]
        (trash / "old").mkdir(parents=True)
        (trash / "old" / "x").write_text("y")
        for name, days in (("old.jsonl", 31), ("recent.jsonl", 29)):
            (trash / name).write_text("{}\n")
            self._aged(trash / name, days)
        self._aged(trash / "old", 31)

        session_trash.purge_trash()

        assert not (trash / "old.jsonl").exists() and not (trash / "old").exists()
        assert (trash / "recent.jsonl").exists()

    def test_no_trash_yet(self, dirs) -> None:
        session_trash.purge_trash()


class TestPicker:
    def _labels(self, keyboard) -> list[str]:
        return [b.text for row in keyboard.inline_keyboard for b in row]

    def test_resume_offers_delete_and_bin_only_when_useful(self) -> None:
        _, keyboard = build_session_picker(SESSIONS, has_trash=True)
        assert "🗑 Delete…" in self._labels(keyboard)
        assert "♻️ Recycle Bin" in self._labels(keyboard)
        _, keyboard = build_session_picker([], has_trash=False)
        assert self._labels(keyboard) == ["➕ New Session", "Cancel"]

    @pytest.mark.parametrize(
        ("mode", "prefix"),
        [(PICKER_DELETE, CB_SESSION_DEL), (PICKER_TRASH, CB_SESSION_RESTORE)],
    )
    def test_buttons_carry_the_session_id(self, mode: str, prefix: str) -> None:
        _, keyboard = build_session_picker(SESSIONS, mode=mode)
        data = keyboard.inline_keyboard[0][0].callback_data
        assert data == f"{prefix}{SID}"
        assert len(data.encode()) <= 64
