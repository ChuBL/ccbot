"""Tests for the file outbox behind `ccbot send`."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ccbot import outbox


@pytest.fixture(autouse=True)
def ccbot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ccbot-home"
    monkeypatch.setenv("CCBOT_DIR", str(home))
    return home


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7")
    return path


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestValidateFile:
    def test_regular_file_is_accepted(self, sample_file: Path) -> None:
        assert outbox.validate_file(sample_file) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert "not found" in (outbox.validate_file(tmp_path / "nope") or "")

    def test_directory(self, tmp_path: Path) -> None:
        assert "Not a regular file" in (outbox.validate_file(tmp_path) or "")

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty"
        path.touch()
        assert "empty" in (outbox.validate_file(path) or "")

    def test_file_over_the_telegram_limit(
        self, sample_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(outbox, "MAX_FILE_BYTES", 4)
        assert "too large" in (outbox.validate_file(sample_file) or "")


class TestCurrentWindowKey:
    def test_outside_tmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert outbox.current_window_key() is None

    def test_inside_tmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMUX_PANE", "%3")
        done = subprocess.CompletedProcess([], 0, stdout="ccbot:@5\n", stderr="")
        with patch("ccbot.outbox.subprocess.run", return_value=done):
            assert outbox.current_window_key() == "ccbot:@5"

    def test_unknown_pane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TMUX_PANE", "%3")
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="can't find pane")
        with patch("ccbot.outbox.subprocess.run", return_value=done):
            assert outbox.current_window_key() is None


class TestClaimRequests:
    def test_no_outbox_directory_yet(self) -> None:
        assert outbox.claim_requests() == []

    def test_claim_returns_the_request_and_removes_it(self, sample_file: Path) -> None:
        request_id = outbox.submit_request("ccbot:@5", sample_file, "the report")

        assert outbox.claim_requests() == [
            outbox.OutboxRequest(request_id, "ccbot:@5", str(sample_file), "the report")
        ]
        assert outbox.claim_requests() == []

    def test_oldest_request_comes_first(self, sample_file: Path) -> None:
        newer = outbox.submit_request("ccbot:@5", sample_file)
        older = outbox.submit_request("ccbot:@5", sample_file)
        _age(outbox.outbox_dir() / f"{older}.json", 5)

        claimed = [request.request_id for request in outbox.claim_requests()]
        assert claimed == [older, newer]

    def test_stale_request_is_dropped_unsent(self, sample_file: Path) -> None:
        request_id = outbox.submit_request("ccbot:@5", sample_file)
        request_file = outbox.outbox_dir() / f"{request_id}.json"
        _age(request_file, outbox.REQUEST_TTL + 1)

        assert outbox.claim_requests() == []
        assert not request_file.exists()

    def test_malformed_request_gets_an_error_result(self) -> None:
        outbox.outbox_dir().mkdir(parents=True)
        (outbox.outbox_dir() / "abc.json").write_text('{"path": "/tmp/x"}')

        assert outbox.claim_requests() == []
        result = json.loads((outbox.outbox_dir() / "abc.result").read_text())
        assert result == {"ok": False, "error": "malformed request"}

    def test_only_old_results_are_removed(self) -> None:
        outbox.write_result("fresh", True)
        outbox.write_result("old", True)
        _age(outbox.outbox_dir() / "old.result", outbox.RESULT_TTL + 1)

        outbox.claim_requests()

        assert (outbox.outbox_dir() / "fresh.result").exists()
        assert not (outbox.outbox_dir() / "old.result").exists()


class TestWaitForResult:
    def test_returns_the_result_and_removes_it(self, sample_file: Path) -> None:
        request_id = outbox.submit_request("ccbot:@5", sample_file)
        outbox.claim_requests()
        outbox.write_result(request_id, False, "no topic bound")

        assert outbox.wait_for_result(request_id, timeout=5) == (
            False,
            "no topic bound",
        )
        assert not (outbox.outbox_dir() / f"{request_id}.result").exists()

    def test_unclaimed_request_is_withdrawn(
        self, sample_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(outbox, "PICKUP_TIMEOUT", 0.0)
        request_id = outbox.submit_request("ccbot:@5", sample_file)

        ok, error = outbox.wait_for_result(request_id, timeout=5)

        assert not ok
        assert "did not pick up" in error
        assert outbox.claim_requests() == []

    def test_times_out_when_a_claimed_request_gets_no_result(
        self, sample_file: Path
    ) -> None:
        request_id = outbox.submit_request("ccbot:@5", sample_file)
        outbox.claim_requests()

        ok, error = outbox.wait_for_result(request_id, timeout=0.3)

        assert not ok
        assert "no result" in error


class TestSendMain:
    def _run(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
        monkeypatch.setattr(sys, "argv", ["ccbot", "send", *args])
        with pytest.raises(SystemExit) as exit_info:
            outbox.send_main()
        return int(exit_info.value.code or 0)

    def test_fails_outside_tmux(
        self, sample_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert self._run(monkeypatch, str(sample_file)) == 1
        assert outbox.claim_requests() == []

    def test_queues_each_file_with_the_caption(
        self, sample_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            patch("ccbot.outbox.current_window_key", return_value="ccbot:@5"),
            patch("ccbot.outbox.wait_for_result", return_value=(True, "")),
        ):
            code = self._run(monkeypatch, str(sample_file), "--caption", "hi")

        assert code == 0
        [request] = outbox.claim_requests()
        assert (request.window_key, request.path, request.caption) == (
            "ccbot:@5",
            str(sample_file),
            "hi",
        )

    def test_invalid_file_is_not_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch("ccbot.outbox.current_window_key", return_value="ccbot:@5"):
            code = self._run(monkeypatch, str(tmp_path / "missing.txt"))

        assert code == 1
        assert outbox.claim_requests() == []

    def test_delivery_error_sets_the_exit_code(
        self, sample_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with (
            patch("ccbot.outbox.current_window_key", return_value="ccbot:@5"),
            patch("ccbot.outbox.wait_for_result", return_value=(False, "no topic")),
        ):
            assert self._run(monkeypatch, str(sample_file)) == 1
