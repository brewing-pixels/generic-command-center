from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import (
    FakeAgent,
    FakeInquisitor,
    FakeProbe,
    FakeTaskStore,
    commit,
    git,
    make_project,
)

from command_center import db, gitutil, spec
from command_center.config import Config
from command_center.eligibility import EligibilityService
from command_center.errors import RunError, ShapeError
from command_center.events import EventBus
from command_center.runner import RunEngine
from command_center.shape import ShapeEngine, ShapeRecord, check_shape, load_tasks
from command_center.taskstore import TaskStoreObjective, TaskStoreTask

TASKS_DRAFT = """version: 1
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

TASKS_SEEDED = """version: 1
objectives:
  - slug: hello-service
    title: Hello Service
    acceptance:
      - the service answers
    gate:
      - ["true"]
    tasks:
      - slug: write-endpoint
        title: Add the greeting endpoint
        acceptance:
          - the endpoint returns a greeting
        gate:
          - ["true"]
        depends_on: []
        scope:
          - "docs/reference/**"
"""

SPEC = "# Shape spec\n\nThis project builds one thing.\n"
QUESTIONS_OPEN = """version: 1
questions:
  - id: q1
    question: Should the thing be called hello?
    default: "yes"
    answer: null
"""
QUESTIONS_ANSWERED = """version: 1
questions:
  - id: q1
    question: Should the thing be called hello?
    default: "yes"
    answer: "yes"
"""
QUESTIONS_EMPTY = "version: 1\nquestions: []\n"


def shape_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "SPEC.md").write_text("# Legacy\n", encoding="utf-8")
    (root / "command-center.yaml").write_text("version: 1\n", encoding="utf-8")
    commit(root, "init")
    return root


def test_spec_check_questions_todo_and_materialized(tmp_path: Path) -> None:
    root = shape_repo(tmp_path)
    docs = root / "docs" / "spec"
    docs.mkdir(parents=True)
    (docs / "SPEC.md").write_text(SPEC, encoding="utf-8")
    (docs / "TASKS.yaml").write_text(TASKS_DRAFT, encoding="utf-8")
    (docs / "QUESTIONS.yaml").write_text(QUESTIONS_OPEN, encoding="utf-8")
    commit(root, "shape draft")

    failed = check_shape(root)
    assert not failed.ok
    assert any("q1" in issue for issue in failed.issues)

    (docs / "QUESTIONS.yaml").write_text(QUESTIONS_ANSWERED, encoding="utf-8")
    commit(root, "answer")
    passed = check_shape(root)
    assert passed.ok, passed.issues
    assert passed.materialized is False

    (docs / "SPEC.md").write_text(SPEC + "\nTODO: decide this\n", encoding="utf-8")
    commit(root, "todo")
    todo = check_shape(root)
    assert not todo.ok
    assert any("TBD/TODO" in issue for issue in todo.issues)


def test_spec_check_materialized_shape(tmp_path: Path) -> None:
    root = shape_repo(tmp_path)
    docs = root / "docs" / "spec"
    docs.mkdir(parents=True)
    (docs / "SPEC.md").write_text(SPEC, encoding="utf-8")
    (docs / "TASKS.yaml").write_text(
        TASKS_DRAFT.replace(
            "    tasks:",
            "    taskstore_id: o_1\n    tasks:",
        ).replace(
            "        scope:",
            "        taskstore_id: t_1\n        scope:",
        ),
        encoding="utf-8",
    )
    (docs / "QUESTIONS.yaml").write_text(QUESTIONS_EMPTY, encoding="utf-8")
    (root / "command-center.yaml").write_text(
        """version: 1
objectives:
  o_1:
    gate:
      - ["true"]
tasks:
  t_1:
    gate:
      - ["true"]
    depends_on: []
    scope: ["hello.txt"]
""",
        encoding="utf-8",
    )
    commit(root, "materialized")
    result = check_shape(root, require_materialized=True)
    assert result.ok, result.issues
    assert result.materialized is True


def test_shape_start_revise_materialize_approve(tmp_path: Path, config: Config) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
            },
            {
                "docs/spec/SPEC.md": SPEC + "\nThe thing is named hello.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    with pytest.raises(RunError):
        RunEngine(config, taskstore, bus=EventBus(), agent=FakeAgent()).create("t1")
    engine.draft(record)
    record = engine.status("p1")["shape"]
    active = ShapeRecord.model_validate(record)
    assert active.state == "questions"
    assert active.worktree

    engine.answer("p1", "q1", "yes")
    record = engine.status("p1")["shape"]
    active = ShapeRecord.model_validate(record)
    assert active.state == "answers_ready"

    engine.revise(active)
    record = engine.status("p1")["shape"]
    active = ShapeRecord.model_validate(record)
    assert active.state == "ready", record
    assert inquisitor.calls == 2
    assert inquisitor.sessions == [None, "ses_arch"]

    materialized = engine.materialize("p1")
    assert materialized.state == "materialized"
    project = taskstore.get_project("p1")
    assert project.spec_state is not None
    assert project.spec_state.phase == "shape"
    assert len(project.objectives) >= 1
    all_tasks = [task for objective in project.objectives for task in objective.tasks]
    assert any(task.title == "Create hello" for task in all_tasks)

    approved = engine.approve("p1")
    assert approved.state == "approved"
    assert approved.approved_commit
    project = taskstore.get_project("p1")
    assert project.spec_state is not None
    assert project.spec_state.phase == "approved"
    assert project.spec_state.docs == ["docs/spec/SPEC.md"]
    assert gitutil.head_commit(root, "cc/integration") == approved.approved_commit
    assert not Path(approved.worktree).exists()
    assert spec.check(project).ok
    plan = gitutil.blob(root, "cc/integration", "command-center.yaml").decode()
    assert "o_quick_" in plan
    assert "t_quick_" in plan

    conn = db.connect(config.state_dir)
    try:
        checks = EligibilityService(conn, taskstore, config, FakeProbe()).check_all()
    finally:
        conn.close()
    hello = next(check for check in checks if check.task.title == "Create hello")
    assert hello.result.ready, hello.result.reasons


def test_shape_cancel_removes_worktree(tmp_path: Path, config: Config) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    engine = ShapeEngine(
        config,
        taskstore,
        agent=FakeInquisitor(
            [
                {
                    "docs/spec/SPEC.md": SPEC,
                    "docs/spec/TASKS.yaml": TASKS_DRAFT,
                    "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
                }
            ]
        ),
    )
    record = engine.prepare("p1")
    assert Path(record.worktree).is_dir()
    cancelled = engine.cancel("p1")
    assert cancelled.state == "cancelled"
    assert not Path(record.worktree).exists()
    assert db.active_shape_for_project(db.connect(config.state_dir), "p1") is None
    assert taskstore.get_project("p1").spec_state.phase == "shape"


def test_shape_revise_refuses_a_second_inquisitor(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            }
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)
    conn = db.connect(config.state_dir)
    try:
        running = ShapeRecord.model_validate(
            engine.status("p1")["shape"]
        )
        running.state = "revising"
        running.save(conn)
    finally:
        conn.close()

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    with pytest.raises(ShapeError, match="one inquisitor at a time"):
        engine.revise(active)
    with pytest.raises(ShapeError, match="one inquisitor at a time"):
        engine.assert_idle("p1")
    assert inquisitor.calls == 1


def test_shape_revise_recovers_a_stale_run(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
            {
                "docs/spec/SPEC.md": SPEC + "\nSecond pass.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)
    conn = db.connect(config.state_dir)
    try:
        payload = db.get_shape(conn, record.id)
        payload["state"] = "revising"
        payload["updated"] = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        db.save_shape(conn, payload)
    finally:
        conn.close()

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    revised = engine.revise(active)
    assert revised.state == "ready"
    assert inquisitor.calls == 2


TASKS_BROKEN = """version: 1
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
         scope:
          - "hello.txt"
"""


def test_shape_draft_repairs_a_broken_check(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_BROKEN,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    assert active.state == "ready", active.check
    assert inquisitor.calls == 2
    assert inquisitor.sessions == [None, "ses_arch"]
    assert (Path(active.run_dir) / "repair-1.txt").is_file()


def test_shape_draft_stops_repairing_when_only_questions_remain(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
            }
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    assert active.state == "questions"
    assert inquisitor.calls == 1


def test_check_reports_questions_not_a_failed_check(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    engine = ShapeEngine(
        config,
        taskstore,
        agent=FakeInquisitor(
            [
                {
                    "docs/spec/SPEC.md": SPEC,
                    "docs/spec/TASKS.yaml": TASKS_DRAFT,
                    "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
                }
            ]
        ),
    )
    record = engine.prepare("p1")
    engine.draft(record)

    checked, check = engine.check("p1")
    assert not check.ok
    assert checked.state == "questions"
    assert any("has no answer" in issue for issue in check.issues)


def test_shape_revise_repairs_an_unreadable_question_file(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
            {
                "docs/spec/SPEC.md": SPEC + "\nRepaired.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            },
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)

    broken = (
        "version: 1\n"
        "questions:\n"
        "  - id: q1\n"
        "    question: What?\n"
        '    default: "A" (one word)\n'
        "    answer: null\n"
    )
    (Path(record.worktree) / "docs/spec/QUESTIONS.yaml").write_text(
        broken, encoding="utf-8"
    )

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    revised = engine.revise(active)
    assert revised.state == "ready"
    assert inquisitor.calls == 2


def test_shape_seed_adopts_existing_objective_and_task(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    objective = TaskStoreObjective(
        id="o_voice",
        title="Voice & Presence",
        tasks=[
            TaskStoreTask(
                id="t_collect",
                title="Add the greeting endpoint",
            )
        ],
    )
    project = make_project(
        path=str(root),
        objectives=[objective],
        spec_state_value=None,
    )
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_SEEDED,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
            }
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare(
        "p1", objective_id="o_voice", task_id="t_collect"
    )
    assert record.objective_id == "o_voice"
    assert record.task_id == "t_collect"
    engine.draft(record)
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    engine.materialize("p1")
    record = ShapeRecord.model_validate(engine.status("p1")["shape"])
    seeded = load_tasks(Path(record.worktree))
    assert seeded.objectives[0].taskstore_id == "o_voice"
    assert seeded.objectives[0].tasks[0].taskstore_id == "t_collect"

    engine.approve("p1")
    project = taskstore.get_project("p1")
    assert len(project.objectives) == 1
    assert project.objectives[0].id == "o_voice"
    assert [task.id for task in project.objectives[0].tasks] == ["t_collect"]

    conn = db.connect(config.state_dir)
    try:
        checks = EligibilityService(conn, taskstore, config, FakeProbe()).check_all()
    finally:
        conn.close()
    selected = next(check for check in checks if check.task.id == "t_collect")
    assert selected.result.ready, selected.result.reasons


def test_revise_accepts_partial_answers(tmp_path: Path, config: Config) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
            },
            {
                "docs/spec/SPEC.md": SPEC + "\nPartial.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
            },
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)

    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    revised = engine.revise(active)
    assert inquisitor.calls == 2
    assert revised.state in {"questions", "answers_ready", "ready"}


def test_discuss_resumes_the_session_read_only(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": SPEC,
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": QUESTIONS_OPEN,
            }
        ]
    )
    engine = ShapeEngine(config, taskstore, agent=inquisitor)
    record = engine.prepare("p1")
    engine.draft(record)

    result = engine.discuss("p1", "which renderer?", question_id="q1")
    assert result["reply"] == "done"
    assert inquisitor.agents[-1] == "cc-inquisitor-chat"
    assert inquisitor.sessions[-1] == "ses_arch"
    active = ShapeRecord.model_validate(engine.status("p1")["shape"])
    assert active.state == "questions"
    assert active.last_reply == "done"


def test_discuss_refuses_while_the_inquisitor_runs(
    tmp_path: Path, config: Config
) -> None:
    root = shape_repo(tmp_path)
    project = make_project(path=str(root), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    engine = ShapeEngine(
        config,
        taskstore,
        agent=FakeInquisitor(
            [
                {
                    "docs/spec/SPEC.md": SPEC,
                    "docs/spec/TASKS.yaml": TASKS_DRAFT,
                    "docs/spec/QUESTIONS.yaml": QUESTIONS_EMPTY,
                }
            ]
        ),
    )
    record = engine.prepare("p1")
    engine.draft(record)
    conn = db.connect(config.state_dir)
    try:
        running = ShapeRecord.model_validate(engine.status("p1")["shape"])
        running.state = "revising"
        running.save(conn)
    finally:
        conn.close()
    with pytest.raises(ShapeError, match="cannot be discussed"):
        engine.discuss("p1", "hello")
