from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SECRET_ENV = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CC_TOKEN",
}
SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


@dataclass(frozen=True)
class GateCommandResult:
    argv: list[str]
    exit_code: int
    duration_ms: int
    tail: str


@dataclass
class GateResult:
    passed: bool
    commands: list[GateCommandResult] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "commands": [
                {
                    "argv": command.argv,
                    "exit_code": command.exit_code,
                    "duration_ms": command.duration_ms,
                    "tail": command.tail,
                }
                for command in self.commands
            ],
            "started_at": self.started_at.isoformat().replace("+00:00", "Z")
            if self.started_at
            else None,
            "finished_at": self.finished_at.isoformat().replace("+00:00", "Z")
            if self.finished_at
            else None,
        }


def _env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in SECRET_ENV and not key.endswith(SECRET_ENV_SUFFIXES)
    }
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    return env


def run_gate(
    worktree: Path,
    commands: list[list[str]],
    *,
    timeout: int,
    gate_path: Path,
) -> GateResult:
    started = datetime.now(UTC)
    results: list[GateCommandResult] = []
    passed = bool(commands)
    for argv in commands:
        command_start = time.monotonic()
        try:
            process = subprocess.run(
                argv,
                cwd=worktree,
                env=_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code = process.returncode
            tail = (process.stdout + process.stderr)[-8192:]
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output = ""
            if exc.stdout:
                output += str(exc.stdout)
            if exc.stderr:
                output += str(exc.stderr)
            tail = output[-8192:]
        except OSError as exc:
            exit_code = 127
            tail = str(exc)
        duration = int((time.monotonic() - command_start) * 1000)
        results.append(GateCommandResult(argv, exit_code, duration, tail))
        if exit_code != 0:
            passed = False
            break
    result = GateResult(
        passed=passed,
        commands=results,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result
