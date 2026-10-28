from __future__ import annotations

from pathlib import Path

from helpers import make_project

from command_center.cli import _cmd_status, _cmd_tasks, main
from command_center.eligibility import CheckResult, Reason, TaskCheck


def test_version(capsys) -> None:
    assert main(["version"]) == 0
    assert "command-center" in capsys.readouterr().out


def test_models_with_empty_config(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
state_dir: {tmp_path / "state"}
taskstore:
  url: http://127.0.0.1:4180
models: {{}}
roles: {{}}
""",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "models"]) == 0
    output = capsys.readouterr().out
    assert "ID" in output


def test_status_and_tasks_output(capsys) -> None:
    project = make_project()
    objective = project.objectives[0]
    task = objective.tasks[0]
    check = TaskCheck(
        project,
        objective,
        task,
        CheckResult(False, [Reason("no_gate", "missing gate")]),
    )
    assert _cmd_status([check], as_json=False) == 0
    assert "no_gate" in capsys.readouterr().out
    assert _cmd_tasks([check], None, False, False) == 0
    assert "no_gate" in capsys.readouterr().out
