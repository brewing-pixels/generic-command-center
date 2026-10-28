from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeInquisitor, FakeTaskStore, commit, git, make_project

from command_center.config import Config
from command_center.errors import ShapeError
from command_center.shape import ShapeEngine, ShapeRecord, check_shape, load_c4

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
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "SPEC.md").write_text("# Legacy\n", encoding="utf-8")
    commit(root, "init")
    project = make_project(path=str(root), spec_state_value=None)
    engine = ShapeEngine(config, FakeTaskStore([project]), agent=FakeInquisitor([
        {
            "docs/spec/SPEC.md": SPEC,
            "docs/spec/TASKS.yaml": TASKS,
            "docs/spec/QUESTIONS.yaml": QUESTIONS,
        }
    ]))
    record = engine.prepare("p1")
    engine.draft(record)
    return engine, record


def c4_doc() -> dict:
    return {
        "version": 1,
        "levels": {
            "context": {
                "elements": [
                    {
                        "id": "operator",
                        "type": "person",
                        "name": "Operator",
                        "description": "Asks questions by voice.",
                    },
                    {"id": "hello", "type": "system", "name": "Hello"},
                ],
                "relationships": [
                    {
                        "from": "operator",
                        "to": "hello",
                        "label": "says hello",
                    }
                ],
            },
            "container": {"elements": [], "relationships": []},
            "component": {"elements": [], "relationships": []},
            "code": {"elements": [], "relationships": []},
        },
    }


def test_c4_is_optional(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    assert engine.c4("p1")["c4"] is None


def test_save_c4_round_trips(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    result = engine.save_c4("p1", c4_doc())
    assert result["check"]["ok"] is True
    stored = load_c4(Path(record.worktree))
    assert stored is not None
    assert stored.levels.context.elements[0].type == "person"
    assert stored.levels.context.relationships[0].from_ == "operator"
    text = (Path(record.worktree) / "docs/spec/C4.yaml").read_text()
    assert "from: operator" in text


def test_relationship_endpoint_must_exist(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    doc = c4_doc()
    doc["levels"]["context"]["relationships"][0]["to"] = "missing"
    with pytest.raises(ShapeError, match="unknown element"):
        engine.save_c4("p1", doc)


def test_duplicate_element_ids_are_rejected(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    doc = c4_doc()
    doc["levels"]["context"]["elements"][1]["id"] = "operator"
    with pytest.raises(ShapeError, match="duplicate element id"):
        engine.save_c4("p1", doc)


def test_blank_relationship_label_is_rejected(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    doc = c4_doc()
    doc["levels"]["context"]["relationships"][0]["label"] = "   "
    with pytest.raises(ShapeError, match="label"):
        engine.save_c4("p1", doc)


def test_decision_elements_are_allowed(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    doc = c4_doc()
    doc["levels"]["container"] = {
        "elements": [
            {"id": "router", "type": "decision", "name": "Known request?"},
            {"id": "model", "type": "container", "name": "Model service"},
        ],
        "relationships": [
            {"from": "router", "to": "model", "label": "[yes]"},
        ],
    }
    engine.save_c4("p1", doc)
    stored = load_c4(Path(record.worktree))
    assert stored.levels.container.elements[0].type == "decision"


def test_c4_is_checked_and_committed(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_c4("p1", c4_doc())
    check = check_shape(Path(record.worktree))
    assert check.ok, check.issues


def test_derive_c4_creates_when_missing(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/C4.yaml": """version: 1
levels:
  context:
    elements:
      - id: operator
        type: person
        name: Operator
    relationships: []
"""
        }
    )
    engine.derive_c4(record)
    assert "fresh derivation" in engine.agent.prompts[-1]
    assert load_c4(Path(record.worktree)) is not None


def test_derive_c4_updates_an_existing_file(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_c4("p1", c4_doc())
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/C4.yaml": """version: 1
levels:
  context:
    elements:
      - id: operator
        type: person
        name: Operator
      - id: hello
        type: system
        name: Hello
    relationships:
      - from: operator
        to: hello
        label: says hello
"""
        }
    )
    engine.derive_c4(record)
    assert "This is an UPDATE" in engine.agent.prompts[-1]
    assert "Operator" in engine.agent.prompts[-1]


def test_derive_c4_refuses_to_wipe_the_model(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_c4("p1", c4_doc())
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/C4.yaml": "version: 1\nlevels:\n  context:\n"
            "    elements: []\n    relationships: []\n"
        }
    )
    with pytest.raises(ShapeError, match="removed every C4 element"):
        engine.derive_c4(record)
