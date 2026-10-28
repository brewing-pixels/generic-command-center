from __future__ import annotations

from pathlib import Path

import pytest
from helpers import (
    FakeAgent,
    FakeInquisitor,
    FakeTaskStore,
    commit,
    git,
    make_project,
    spec_state,
)

from command_center import spec
from command_center.config import Config, load_config, save_roles
from command_center.errors import ConfigError, ShapeError
from command_center.runner import RunEngine
from command_center.shape import ShapeEngine
from command_center.taskstore import TaskStoreTask

SPEC = "# Shape spec\n"
TASKS = """version: 1
objectives:
  - slug: build
    title: Build
    acceptance:
      - a
    gate:
      - ["true"]
    tasks:
      - slug: hello
        title: Create hello
        acceptance:
          - a
        gate:
          - ["true"]
        depends_on: []
        scope: ["hello.txt"]
"""
QUESTIONS = "version: 1\nquestions: []\n"
DRAFT = {
    "docs/spec/SPEC.md": SPEC,
    "docs/spec/TASKS.yaml": TASKS,
    "docs/spec/QUESTIONS.yaml": QUESTIONS,
}


def shape_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "SPEC.md").write_text("# Legacy\n", encoding="utf-8")
    commit(root, "init")
    return root


def make_shape_engine(tmp_path: Path, config: Config) -> ShapeEngine:
    taskstore = FakeTaskStore(
        [make_project(path=str(shape_repo(tmp_path)), spec_state_value=None)]
    )
    return ShapeEngine(config, taskstore, agent=FakeInquisitor([DRAFT]))


def test_shape_starts_on_a_chosen_model(tmp_path: Path, config: Config) -> None:
    engine = make_shape_engine(tmp_path, config)
    engine.draft(engine.prepare("p1", model="local"))
    assert engine.status("p1")["shape"]["model"] == "local"


def test_shape_model_switch_is_persisted(tmp_path: Path, config: Config) -> None:
    engine = make_shape_engine(tmp_path, config)
    engine.draft(engine.prepare("p1"))
    engine.set_model("p1", "local")
    assert engine.status("p1")["shape"]["model"] == "local"


def test_shape_model_rejects_an_unknown_id(tmp_path: Path, config: Config) -> None:
    engine = make_shape_engine(tmp_path, config)
    engine.draft(engine.prepare("p1"))
    with pytest.raises(ShapeError, match="unknown model"):
        engine.set_model("p1", "gpt-9")


def test_reviewer_override_is_recorded(repo: Path, config: Config) -> None:
    (repo / "command-center.yaml").write_text(
        'version: 1\ntasks:\n  t1:\n    gate:\n      - ["true"]\n    scope:\n      - "src/**"\n',
        encoding="utf-8",
    )
    commit(repo, "plan")
    digest = spec.compute_digest(repo, ["SPEC.md"], "HEAD")
    project = make_project(
        path=str(repo),
        tasks=[TaskStoreTask(id="t1", title="One")],
        spec_state_value=spec_state(["SPEC.md"], digest),
    )
    engine = RunEngine(config, FakeTaskStore([project]), agent=FakeAgent())
    record = engine.create("t1", None, "local")
    assert record.reviewer_model == "local"
    assert record.reviewer_resolved == "local/model-name"


def test_save_roles_round_trips_and_keeps_comments(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "# keep this comment\n"
        "models:\n"
        "  hosted:\n"
        "    label: Hosted\n"
        "    model: provider/model-name\n"
        "  local:\n"
        "    label: Local\n"
        "    model: local/model-name\n"
        "opencode:\n"
        "  chat_server: null\n"
        "roles:\n"
        "  coder: hosted\n",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    save_roles(loaded, {"reviewer": "local", "coder": "local"})
    text = config_path.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "reviewer: local" in text
    assert "chat_server: null" in text
    assert loaded.roles["reviewer"] == "local"
    reloaded = load_config(config_path)
    assert reloaded.roles["coder"] == "local"
    assert reloaded.roles["reviewer"] == "local"


def test_save_roles_refuses_an_unknown_model(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  hosted:\n    label: Hosted\n    model: provider/model-name\n"
        "roles:\n  coder: hosted\n",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    with pytest.raises(ConfigError):
        save_roles(loaded, {"reviewer": "nope"})
