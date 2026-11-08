"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from unittest.mock import AsyncMock

import pytest

from ccbot.config import config
from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import SessionInfo, SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_poison_line_skipped(self, monitor, tmp_path, make_jsonl_entry):
        """A complete (newline-terminated) but corrupt line must be skipped.

        Regression: previously any unparseable line was treated as a partial
        write and retried forever, permanently stalling the session.
        """
        jsonl_file = tmp_path / "session.jsonl"
        good = make_jsonl_entry(msg_type="assistant", content="ok")
        jsonl_file.write_text(
            json.dumps(good) + "\n" + "{corrupt json!!!\n" + json.dumps(good) + "\n",
            encoding="utf-8",
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Both good lines returned; offset advanced past the poison line
        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_partial_line_at_eof_retried(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """An unterminated line at EOF is a partial write — retry next cycle."""
        jsonl_file = tmp_path / "session.jsonl"
        good = make_jsonl_entry(msg_type="assistant", content="ok")
        good_line = json.dumps(good) + "\n"
        jsonl_file.write_text(good_line + '{"type": "assis', encoding="utf-8")
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 1
        # Offset stops at the partial line, not EOF
        assert session.last_byte_offset == len(good_line.encode("utf-8"))

        # Writer completes the line — next cycle picks it up
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write('tant", "message": {"content": "done"}}\n')
        result2 = await monitor._read_new_lines(session, jsonl_file)
        assert len(result2) == 1
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestSessionStarts:
    """A session started while the bot watches: first prompt kept, topic told."""

    WINDOW = "@1"
    SID = "11111111-2222-3333-4444-555555555555"

    @pytest.fixture
    def monitor(self, tmp_path, monkeypatch):
        map_file = tmp_path / "session_map.json"
        monkeypatch.setattr(config, "session_map_file", map_file)
        monkeypatch.setattr(config, "tmux_session_name", "T")
        mon = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        mon.started = []

        async def on_start(window_id, session_id, source):
            mon.started.append((window_id, session_id, source))

        mon.set_session_start_callback(on_start)
        return mon

    def _write_map(self, source, started_at, sid=None):
        config.session_map_file.write_text(
            json.dumps(
                {
                    f"T:{self.WINDOW}": {
                        "session_id": sid or self.SID,
                        "cwd": "/tmp",
                        "window_name": "w",
                        "source": source,
                        "started_at": started_at,
                    }
                }
            )
        )

    async def _tick(self, mon):
        current = await mon._detect_and_cleanup_changes()
        await mon._handle_session_starts()
        return await mon.check_for_updates(set(current.values()))

    async def _first_sight(self, mon, tmp_path, make_jsonl_entry, source):
        """Run the poll that announces a start, then the one that finds its file."""
        self._write_map(source, 100.0)
        await self._tick(mon)
        jsonl = tmp_path / f"{self.SID}.jsonl"
        jsonl.write_text(
            json.dumps(make_jsonl_entry(msg_type="user", content="first prompt")) + "\n"
        )
        mon.scan_projects = AsyncMock(
            return_value=[SessionInfo(session_id=self.SID, file_path=jsonl)]
        )
        await self._tick(mon)
        return await self._tick(mon), jsonl

    @pytest.mark.asyncio
    async def test_new_session_keeps_first_prompt(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        monitor.scan_projects = AsyncMock(return_value=[])
        msgs, _ = await self._first_sight(
            monitor, tmp_path, make_jsonl_entry, "startup"
        )
        assert [m.text for m in msgs] == ["first prompt"]
        assert monitor.started == [(self.WINDOW, self.SID, "startup")]

    @pytest.mark.asyncio
    async def test_resumed_session_skips_history(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        monitor.scan_projects = AsyncMock(return_value=[])
        msgs, _ = await self._first_sight(monitor, tmp_path, make_jsonl_entry, "resume")
        assert msgs == []
        assert monitor.started == [(self.WINDOW, self.SID, "resume")]

    @pytest.mark.asyncio
    async def test_same_id_restart_announced_once_each(self, monitor):
        monitor.scan_projects = AsyncMock(return_value=[])
        self._write_map("resume", 100.0)
        await self._tick(monitor)
        await self._tick(monitor)
        self._write_map("resume", 200.0)
        await self._tick(monitor)
        assert len(monitor.started) == 2

    @pytest.mark.asyncio
    async def test_clear_is_fresh_but_silent(self, monitor):
        monitor.scan_projects = AsyncMock(return_value=[])
        self._write_map("clear", 100.0)
        await self._tick(monitor)
        assert monitor.started == []
        assert self.SID in monitor._fresh_sessions

    @pytest.mark.asyncio
    async def test_map_without_source_is_silent(self, monitor):
        """The agent picker's manual `ccbot hook` carries no source."""
        monitor.scan_projects = AsyncMock(return_value=[])
        self._write_map("", 100.0)
        await self._tick(monitor)
        assert monitor.started == []
        assert monitor._fresh_sessions == set()
