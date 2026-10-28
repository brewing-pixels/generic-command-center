from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from argparse import Namespace
from pathlib import Path

from command_center.agent import OpenCodeAgent, _find_session, _find_text
from command_center.cli import _cmd_agents
from command_center.config import Config


def test_session_and_text_extraction() -> None:
    event = {
        "sessionID": "ses_abc",
        "parts": [{"type": "text", "text": "done"}],
    }
    assert _find_session(event) == "ses_abc"
    assert _find_text(event) == "done"
    assert _find_session({"nested": [{"session_id": "ses_nested"}]}) == "ses_nested"


def test_agent_install(tmp_path: Path) -> None:
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    destination = tmp_path / "agents"
    assert _cmd_agents(config, Namespace(dest=str(destination))) == 0
    assert (destination / "cc-coder.md").is_file()
    assert (destination / "cc-reviewer.md").is_file()
    assert (destination / "cc-inquisitor.md").is_file()
    assert (destination / "cc-chat.md").is_file()


def test_agent_resumes_a_session(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class FakeProcess:
        pid = 12348

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                '{"sessionID":"ses_prev","parts":[{"type":"text","text":"ok"}]}\n'
            )
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    result = OpenCodeAgent(config).run(
        "prompt",
        cwd=tmp_path,
        run_dir=tmp_path / "run",
        model="m",
        session_id="ses_prev",
    )
    command = captured["command"]
    assert command[command.index("--session") + 1] == "ses_prev"
    assert command[-1] == "prompt"
    assert result.session_id == "ses_prev"


def test_agent_starts_fresh_without_a_session(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class FakeProcess:
        pid = 12349

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                '{"sessionID":"ses_new","parts":[{"type":"text","text":"ok"}]}\n'
            )
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    OpenCodeAgent(config).run(
        "prompt", cwd=tmp_path, run_dir=tmp_path / "run", model="m"
    )
    assert "--session" not in captured["command"]


def test_agent_sets_worktree_pwd(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    class FakeProcess:
        pid = 12345

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                '{"sessionID":"ses_x","parts":[{"type":"text","text":"done"}]}\n'
            )
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = OpenCodeAgent(config).run(
        "prompt",
        cwd=worktree,
        run_dir=tmp_path / "run",
        model="provider/model-name",
    )
    assert result.exit_code == 0
    assert captured["cwd"] == worktree.resolve()
    assert captured["env"]["PWD"] == str(worktree.resolve())


def test_agent_writes_log_while_running(tmp_path: Path, monkeypatch) -> None:
    class SlowProcess:
        pid = 12346

        def __init__(self) -> None:
            read_fd, write_fd = os.pipe()
            self._write_fd = write_fd
            os.write(
                write_fd,
                b'{"sessionID":"ses_stream","parts":[{"type":"text","text":"first"}]}\n',
            )
            self.stdout = os.fdopen(read_fd, "r")
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            time.sleep(0.4)
            os.write(
                self._write_fd,
                b'{"parts":[{"type":"text","text":"second"}]}\n',
            )
            os.close(self._write_fd)
            return 0

    def fake_popen(command, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    run_dir = tmp_path / "run"
    box: dict = {}

    def run() -> None:
        box["result"] = OpenCodeAgent(config).run(
            "prompt",
            cwd=tmp_path,
            run_dir=run_dir,
            model="provider/model-name",
        )

    thread = threading.Thread(target=run)
    thread.start()
    log = run_dir / "agent.jsonl"
    deadline = time.time() + 2
    while time.time() < deadline:
        if log.is_file() and '"first"' in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.01)
    assert '"first"' in log.read_text(encoding="utf-8")
    assert thread.is_alive()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert box["result"].exit_code == 0
    assert box["result"].final_text == "second"


def test_agent_appends_reviewer_stream(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        pid = 12347

        def __init__(self) -> None:
            self.stdout = io.StringIO(
                '{"sessionID":"ses_x","parts":[{"type":"text","text":"line"}]}\n'
            )
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProcess())
    config = Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
    )
    agent = OpenCodeAgent(config)
    run_dir = tmp_path / "run"
    agent.run("coder", cwd=tmp_path, run_dir=run_dir, model="m")
    agent.run("reviewer", cwd=tmp_path, run_dir=run_dir, model="m", append=True)
    lines = (run_dir / "agent.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
