from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeInquisitor, FakeTaskStore, make_project

from command_center.config import Config
from command_center.errors import ShapeError
from command_center.shape import ShapeEngine, load_tasks

TASKS = """version: 1
objectives:
  - slug: build
    title: Build the thing
    acceptance:
      - the thing exists
    gate:
      - ["true"]
    tasks:
      - slug: hello
        title: Create hello
        acceptance:
          - hello exists
        gate:
          - ["true"]
        depends_on: []
        scope:
          - "hello.txt"
"""

SPEC = "# Shape spec\n"
QUESTIONS = "version: 1\nquestions: []\n"


def make_shape(tmp_path: Path, config: Config):
    root = tmp_path / "repo"
    root.mkdir()
    from helpers import commit, git

    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "SPEC.md").write_text("# Legacy\n", encoding="utf-8")
    commit(root, "init")
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS,
                "docs/spec/QUESTIONS.yaml": QUESTIONS,
            }
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)
    return engine, root, record


def test_tree_returns_the_decomposition(tmp_path: Path, config: Config) -> None:
    engine, _, _ = make_shape(tmp_path, config)
    payload = engine.tree("p1")
    assert payload["state"] == "ready"
    assert payload["editable"] is True
    objective = payload["tree"]["objectives"][0]
    assert objective["slug"] == "build"
    assert objective["tasks"][0]["slug"] == "hello"
    assert payload["check"]["ok"] is True


def test_save_tree_adds_a_task(tmp_path: Path, config: Config) -> None:
    engine, _, _ = make_shape(tmp_path, config)
    tree = engine.tree("p1")["tree"]
    task = tree["objectives"][0]["tasks"][0]
    tree["objectives"][0]["tasks"].append(
        {
            **task,
            "slug": "goodbye",
            "title": "Create goodbye",
            "depends_on": ["hello"],
        }
    )
    result = engine.save_tree("p1", tree)
    assert [row["slug"] for row in result["tree"]["objectives"][0]["tasks"]] == [
        "hello",
        "goodbye",
    ]
    assert result["check"]["ok"] is True
    assert result["state"] == "ready"


def test_save_tree_rejects_a_cycle(tmp_path: Path, config: Config) -> None:
    engine, _, record = make_shape(tmp_path, config)
    tree = engine.tree("p1")["tree"]
    tasks = tree["objectives"][0]["tasks"]
    tasks.append({**tasks[0], "slug": "second", "title": "Second"})
    tasks[0]["depends_on"] = ["second"]
    tasks[1]["depends_on"] = ["hello"]
    with pytest.raises(ShapeError, match="cycle"):
        engine.save_tree("p1", tree)
    stored = load_tasks(Path(record.worktree))
    assert [task.slug for task in stored.objectives[0].tasks] == ["hello"]


def test_save_tree_reports_field_errors(tmp_path: Path, config: Config) -> None:
    engine, _, _ = make_shape(tmp_path, config)
    tree = engine.tree("p1")["tree"]
    tree["objectives"][0]["tasks"][0]["acceptance"] = []
    with pytest.raises(ShapeError, match="acceptance"):
        engine.save_tree("p1", tree)


def test_tree_is_read_only_after_approval(tmp_path: Path, config: Config) -> None:
    engine, _, record = make_shape(tmp_path, config)
    engine.materialize("p1")
    engine.approve("p1")
    with pytest.raises(ShapeError):
        engine.save_tree("p1", {"version": 1, "objectives": []})
