from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass(frozen=True)
class AgentResult:
    session_id: str | None
    final_text: str
    exit_code: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    context: int = 0
    error: str | None = None


def _find_session(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("sessionID", "session_id", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            found = _find_session(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_session(nested)
            if found:
                return found
    return None


def _find_text(value: object) -> str | None:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            return content
        for nested in value.values():
            found = _find_text(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_text(nested)
            if found:
                return found
    return None


class OpenCodeAgent:
    def __init__(self, config: Config, timeout: int | None = None):
        self.config = config
        self.timeout = timeout or config.budgets.agent_timeout_seconds
        self._process: subprocess.Popen[str] | None = None

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        run_dir: Path,
        model: str,
        agent: str = "cc-coder",
        append: bool = False,
        session_id: str | None = None,
    ) -> AgentResult:
        command = [
            self.config.opencode.binary,
            "run",
            "--agent",
            agent,
            "--model",
            model,
            "--format",
            "json",
            "--auto",
        ]
        if session_id:
            command += ["--session", session_id]
        command.append(prompt)
        cwd = Path(cwd).resolve()
        env = os.environ.copy()
        env["PWD"] = str(cwd)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "agent.jsonl"
        stderr_path = run_dir / "agent.stderr.log"
        mode = "a" if append else "w"
        exit_code = 127
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        timed_out = False
        process: subprocess.Popen[str] | None = None

        try:
            with stdout_path.open(mode, encoding="utf-8") as stdout_file, stderr_path.open(
                mode, encoding="utf-8"
            ) as stderr_file:
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        start_new_session=True,
                    )
                    self._process = process
                except OSError as exc:
                    stderr_file.write(str(exc) + "\n")
                    stderr_file.flush()
                    empty = ""
                    return AgentResult(None, empty, 127)

                def read_stream(pipe, sink, lines: list[str]) -> None:
                    try:
                        assert pipe is not None
                        for line in iter(pipe.readline, ""):
                            sink.write(line)
                            sink.flush()
                            lines.append(line)
                    finally:
                        if pipe is not None:
                            pipe.close()

                stdout_thread = threading.Thread(
                    target=read_stream,
                    args=(process.stdout, stdout_file, stdout_lines),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=read_stream,
                    args=(process.stderr, stderr_file, stderr_lines),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                try:
                    exit_code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = 124
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                stdout_thread.join(timeout=10)
                stderr_thread.join(timeout=10)
                self._process = None
                if timed_out:
                    stderr_file.write("\nagent timed out\n")
                    stderr_file.flush()
        except OSError as exc:
            run_dir.mkdir(parents=True, exist_ok=True)
            stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
            return AgentResult(None, "", 127)

        session_id: str | None = None
        final_text = ""
        tokens_in = 0
        tokens_out = 0
        cost = 0.0
        context = 0
        agent_error: str | None = None
        for line in stdout_lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = session_id or _find_session(event)
            text = _find_text(event)
            if text:
                final_text = text
            if event.get("type") == "error":
                payload = event.get("error") or {}
                message = payload.get("message") if isinstance(payload, dict) else None
                agent_error = str(message or payload or "agent error")
            if event.get("type") == "step_finish":
                part = event.get("part") or {}
                tokens = part.get("tokens") or {}
                cache = tokens.get("cache") or {}
                # input excludes the cached prefix; the prompt we actually sent
                # is input + cache.read + cache.write. Using input alone makes
                # a 300k session look like a 144-token one.
                prompt_tokens = (
                    int(tokens.get("input") or 0)
                    + int(cache.get("read") or 0)
                    + int(cache.get("write") or 0)
                )
                tokens_in += prompt_tokens
                tokens_out += int(tokens.get("output") or 0)
                cost += float(part.get("cost") or 0)
                context = prompt_tokens
        if not final_text:
            final_text = next(
                (
                    line
                    for line in reversed(stdout_lines)
                    if line.strip()
                ),
                "",
            )
        return AgentResult(
            session_id,
            final_text,
            exit_code,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            context=context,
            error=agent_error,
        )

    def terminate(self) -> None:
        """Kill the turn in flight, if any. Safe to call from another thread."""
        process = self._process
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
