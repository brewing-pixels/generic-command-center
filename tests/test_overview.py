from __future__ import annotations

from pathlib import Path

from helpers import FakeTaskStore, make_agenda, make_project, work_item

from command_center import db
from command_center.config import Config
from command_center.overview import build_overview
from command_center.runs import RunRecord, RunState
from command_center.shape import ShapeRecord
from command_center.taskstore import TaskStoreTask

QUESTIONS = """version: 1
questions:
  - id: q1
    question: Which renderer hosts the avatar?
    default: three.js
    answer: null
"""


def shape_in_questions(conn, worktree: Path, project: str = "p1") -> None:
    docs = worktree / "docs" / "spec"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "QUESTIONS.yaml").write_text(QUESTIONS, encoding="utf-8")
    record = ShapeRecord.new(
        project=project,
        branch="shape-1",
        worktree=str(worktree),
        run_dir=str(worktree / "run"),
        base_sha="0" * 40,
        state="questions",
    )
    record.save(conn)


def test_overview_lists_shape_questions(tmp_path: Path, config: Config) -> None:
    project = make_project(tasks=[TaskStoreTask(id="t1", title="One")])
    taskstore = FakeTaskStore([project])
    conn = db.connect(config.state_dir)
    try:
        shape_in_questions(conn, tmp_path / "wt")
        rows = build_overview(taskstore, conn, config)
    finally:
        conn.close()

    shape_row = next(row for row in rows if row["kind"] == "shape")
    assert shape_row["severity"] == "high"
    assert shape_row["actions"][0]["id"] == "answer"
    assert [q["id"] for q in shape_row["questions"]] == ["q1"]


def test_overview_lists_blocked_runs(tmp_path: Path, config: Config) -> None:
    project = make_project(tasks=[TaskStoreTask(id="t1", title="One")])
    taskstore = FakeTaskStore([project])
    conn = db.connect(config.state_dir)
    try:
        run = RunRecord.new(
            project="p1",
            objective="o_general",
            task="t1",
            model_requested="hosted",
            model_resolved="provider/model-name",
            provider="hosted",
            reviewer_model="hosted",
            reviewer_resolved="provider/model-name",
        )
        run.state = RunState.blocked
        run.verdict = "gate_failed"
        run.notes = ["gate failed: pytest (exit 1)"]
        run.save(conn)
        rows = build_overview(taskstore, conn, config)
    finally:
        conn.close()

    run_row = next(row for row in rows if row["kind"] == "run")
    assert run_row["run"] == run.id
    assert [action["id"] for action in run_row["actions"]] == ["inspect", "retry"]
    assert "gate failed" in run_row["detail"]


def test_overview_asks_for_an_agenda(tmp_path: Path, config: Config) -> None:
    project = make_project(tasks=[TaskStoreTask(id="t1", title="One")])
    taskstore = FakeTaskStore([project])
    conn = db.connect(config.state_dir)
    try:
        rows = build_overview(taskstore, conn, config)
    finally:
        conn.close()

    assert any(row["id"] == "agenda:none" for row in rows)
    agenda_row = next(row for row in rows if row["id"] == "agenda:none")
    assert agenda_row["actions"][0]["id"] == "compose"


def test_overview_asks_to_lock_a_draft(tmp_path: Path, config: Config) -> None:
    project = make_project(tasks=[TaskStoreTask(id="t1", title="One")])
    agenda = make_agenda(items=[work_item()])
    taskstore = FakeTaskStore([project], agenda)
    conn = db.connect(config.state_dir)
    try:
        rows = build_overview(taskstore, conn, config)
    finally:
        conn.close()

    draft = next(row for row in rows if row["id"] == "agenda:draft")
    assert draft["actions"][0]["id"] == "lock"


def test_overview_flags_agenda_items_that_cannot_build(
    repo: Path, config: Config
) -> None:
    project = make_project(path=str(repo), tasks=[TaskStoreTask(id="t1", title="One")])
    agenda = make_agenda(items=[work_item()])
    agenda = agenda.model_copy(update={"status": "locked"})
    taskstore = FakeTaskStore([project], agenda)
    conn = db.connect(config.state_dir)
    try:
        rows = build_overview(taskstore, conn, config)
    finally:
        conn.close()

    blocked = next(row for row in rows if row["id"] == "agenda:a_01")
    assert blocked["severity"] == "high"
    assert blocked["actions"][0]["id"] == "open"
    assert "no_gate" in blocked["detail"] or "spec_unapproved" in blocked["detail"]
