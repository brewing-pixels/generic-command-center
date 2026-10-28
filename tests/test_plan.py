from __future__ import annotations

from pathlib import Path

import pytest

from command_center.errors import PlanError
from command_center.plan import load_plan


def write_plan(root: Path, text: str) -> None:
    (root / "command-center.yaml").write_text(text, encoding="utf-8")


def test_gate_resolution_order(tmp_path: Path) -> None:
    write_plan(
        tmp_path,
        """
version: 1
defaults:
  gate:
    - ["echo", "default"]
project:
  gate:
    - ["echo", "project"]
objectives:
  o1:
    gate:
      - ["echo", "objective"]
tasks:
  t1:
    gate:
      - ["echo", "task"]
  t2:
    depends_on: [t1]
""",
    )
    plan = load_plan(tmp_path)
    assert plan.gate_for("o1", "t1") == [["echo", "task"]]
    assert plan.gate_for("o1", "t2") == [["echo", "objective"]]
    assert plan.gate_for("o2", "t3") == [["echo", "project"]]
    assert plan.deps_for("t2") == ["t1"]
    assert plan.deps_for("t1") == []


def test_default_gate_is_last(tmp_path: Path) -> None:
    write_plan(
        tmp_path,
        """
version: 1
defaults:
  gate:
    - ["echo", "default"]
""",
    )
    plan = load_plan(tmp_path)
    assert plan.gate_for("o1", "t1") == [["echo", "default"]]


def test_gate_must_be_argv_lists(tmp_path: Path) -> None:
    write_plan(
        tmp_path,
        """
version: 1
tasks:
  t1:
    gate:
      - "uv run pytest"
""",
    )
    with pytest.raises(PlanError):
        load_plan(tmp_path)


def test_missing_and_unsupported_plan(tmp_path: Path) -> None:
    with pytest.raises(PlanError):
        load_plan(tmp_path)
    write_plan(tmp_path, "version: 2\n")
    with pytest.raises(PlanError):
        load_plan(tmp_path)
