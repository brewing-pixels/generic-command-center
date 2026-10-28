from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeInquisitor, FakeTaskStore, commit, git, make_project

from command_center.config import Config
from command_center.errors import ShapeError
from command_center.shape import (
    ShapeEngine,
    ShapeRecord,
    check_shape,
    load_use_cases,
)

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
    return engine, record


def use_case_doc(steps: list[dict[str, str]] | None = None) -> dict:
    return {
        "version": 1,
        "use_cases": [
            {
                "id": "uc1",
                "title": "Say hello",
                "actor": "Operator",
                "summary": "One line.",
                "steps": steps
                or [
                    {"label": "Create hello", "task": "hello"},
                    {"label": "Show the greeting"},
                ],
            }
        ],
    }


def test_use_cases_are_optional(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    payload = engine.use_cases("p1")
    assert payload["use_cases"] is None
    assert payload["editable"] is True
    assert check_shape(Path(record.worktree)).ok


def test_save_use_cases_writes_and_checks(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    result = engine.save_use_cases("p1", use_case_doc())
    assert result["state"] == "ready"
    assert result["check"]["ok"] is True
    assert result["editable"] is True

    stored = load_use_cases(Path(record.worktree))
    assert stored is not None
    assert stored.use_cases[0].id == "uc1"
    assert stored.use_cases[0].steps[0].task == "hello"
    assert stored.use_cases[0].steps[1].task is None


def test_save_use_cases_rejects_an_unknown_task(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    with pytest.raises(ShapeError, match="unknown task"):
        engine.save_use_cases(
            "p1", use_case_doc([{"label": "Do a thing", "task": "missing"}])
        )
    assert load_use_cases(Path(record.worktree)) is None


def test_save_use_cases_rejects_duplicate_ids(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    payload = use_case_doc()
    payload["use_cases"].append(
        {
            "id": "uc1",
            "title": "Second flow",
            "steps": [{"label": "Start"}],
        }
    )
    with pytest.raises(ShapeError, match="duplicate use case id"):
        engine.save_use_cases("p1", payload)


def test_save_use_cases_rejects_empty_steps(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    with pytest.raises(ShapeError):
        engine.save_use_cases(
            "p1",
            {
                "version": 1,
                "use_cases": [
                    {"id": "uc9", "title": "Nothing", "steps": []}
                ],
            },
        )


def test_check_flags_a_stale_task_link(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    worktree = Path(record.worktree)
    docs = worktree / "docs" / "spec"
    (docs / "USE_CASES.yaml").write_text(
        """version: 1
use_cases:
  - id: uc1
    title: Say hello
    steps:
      - label: Create hello
        task: gone
"""
        ,
        encoding="utf-8",
    )
    commit(worktree, "stale workflow")
    check = check_shape(worktree)
    assert not check.ok
    assert any("unknown task" in issue for issue in check.issues)


def test_use_cases_are_read_only_after_materialize(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    engine.materialize("p1")
    with pytest.raises(ShapeError, match="read-only"):
        engine.save_use_cases("p1", use_case_doc())


def test_derive_use_cases_writes_the_file(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/USE_CASES.yaml": """version: 1
use_cases:
  - id: uc1
    title: Say hello
    actor: Operator
    steps:
      - label: Create hello
        task: hello
      - label: Show the greeting
"""
        }
    )
    result = engine.derive_use_cases(record)
    assert result.state == "ready"
    stored = load_use_cases(Path(result.worktree))
    assert stored is not None
    assert [step.label for step in stored.use_cases[0].steps] == [
        "Create hello",
        "Show the greeting",
    ]
    assert engine.agent.agents[-1] == "cc-inquisitor"


def test_derive_use_cases_requires_the_file(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append({"docs/spec/SPEC.md": "# unchanged\n"})
    with pytest.raises(ShapeError, match="did not write USE_CASES.yaml"):
        engine.derive_use_cases(record)
    assert engine.status("p1")["shape"]["state"] == "error"


def test_derive_use_cases_rejects_a_bad_link(
    tmp_path: Path, config: Config
) -> None:
    engine, _ = make_shape(tmp_path, config)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/USE_CASES.yaml": """version: 1
use_cases:
  - id: uc1
    title: Say hello
    steps:
      - label: Do something
        task: missing
"""
        }
    )
    with pytest.raises(ShapeError, match="unknown task"):
        engine.derive_use_cases(record)


def choice_doc(target: str = "s3", kind: str = "choice", **extra) -> dict:
    step = {
        "id": "s2",
        "label": "Does the phrase match?",
        "kind": kind,
        "branches": [{"guard": "yes", "to": target}],
        **extra,
    }
    if kind != "choice":
        step.pop("kind")
        step["branches"] = [{"guard": "yes", "to": target}]
    return {
        "version": 1,
        "use_cases": [
            {
                "id": "uc1",
                "title": "Say hello",
                "steps": [
                    {"id": "s1", "label": "Create hello", "task": "hello"},
                    step,
                    {"id": "s3", "label": "Show the greeting"},
                ],
            }
        ],
    }


def test_choice_branches_round_trip(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    result = engine.save_use_cases("p1", choice_doc())
    assert result["check"]["ok"] is True
    stored = load_use_cases(Path(record.worktree))
    assert stored is not None
    step = stored.use_cases[0].steps[1]
    assert step.kind == "choice"
    assert step.branches[0].guard == "yes"
    assert step.branches[0].to == "s3"


def test_branch_must_point_forward(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    with pytest.raises(ShapeError, match="backwards"):
        engine.save_use_cases("p1", choice_doc(target="s1"))


def test_branch_target_must_exist(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    with pytest.raises(ShapeError, match="unknown step"):
        engine.save_use_cases("p1", choice_doc(target="s9"))


def test_branches_only_on_a_choice(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    doc = choice_doc()
    doc["use_cases"][0]["steps"][1].pop("kind")
    with pytest.raises(ShapeError, match="choice"):
        engine.save_use_cases("p1", doc)


def test_duplicate_step_ids_are_rejected(tmp_path: Path, config: Config) -> None:
    engine, _ = make_shape(tmp_path, config)
    doc = choice_doc()
    doc["use_cases"][0]["steps"][2]["id"] = "s2"
    with pytest.raises(ShapeError, match="duplicate step id"):
        engine.save_use_cases("p1", doc)


def test_plain_steps_are_written_without_defaults(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_use_cases("p1", use_case_doc())
    text = (Path(record.worktree) / "docs/spec/USE_CASES.yaml").read_text()
    assert "kind:" not in text
    assert "branches:" not in text


def test_derive_updates_an_existing_file(tmp_path: Path, config: Config) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_use_cases("p1", use_case_doc())
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/USE_CASES.yaml": """version: 1
use_cases:
  - id: uc1
    title: Say hello
    steps:
      - label: Create hello
        task: hello
      - label: Show the greeting
  - id: uc2
    title: Say goodbye
    steps:
      - label: Wave
"""
        }
    )
    engine.derive_use_cases(record)
    prompt = engine.agent.prompts[-1]
    assert "This is an UPDATE" in prompt
    assert "Say hello" in prompt
    stored = load_use_cases(Path(record.worktree))
    assert [use_case.id for use_case in stored.use_cases] == ["uc1", "uc2"]


def test_derive_creates_when_the_file_is_missing(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {
            "docs/spec/USE_CASES.yaml": """version: 1
use_cases:
  - id: uc1
    title: Say hello
    steps:
      - label: Create hello
        task: hello
"""
        }
    )
    engine.derive_use_cases(record)
    assert "fresh derivation" in engine.agent.prompts[-1]


def test_derive_refuses_to_wipe_every_flow(
    tmp_path: Path, config: Config
) -> None:
    engine, record = make_shape(tmp_path, config)
    engine.save_use_cases("p1", use_case_doc())
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.agent.drafts.append(
        {"docs/spec/USE_CASES.yaml": "version: 1\nuse_cases: []\n"}
    )
    with pytest.raises(ShapeError, match="removed every use case"):
        engine.derive_use_cases(record)
