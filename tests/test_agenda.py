from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from helpers import (
    FakeProbe,
    FakeTaskStore,
    commit,
    make_agenda,
    make_project,
    manual_item,
    spec_state,
    work_item,
)

from command_center import db, spec
from command_center.agenda import AgendaService, AgendaValidationError
from command_center.config import Config
from command_center.eligibility import EligibilityService
from command_center.errors import TaskStoreError
from command_center.taskstore import TaskStoreAgendaItem, TaskStoreTask


def setup_agenda(
    repo: Path,
    config: Config,
    *,
    plan: str,
    tasks: list[TaskStoreTask],
    agenda,
    extra_projects=None,
) -> tuple[AgendaService, FakeTaskStore, object]:
    (repo / "command-center.yaml").write_text(plan, encoding="utf-8")
    commit(repo, "plan")
    digest = spec.compute_digest(repo, ["SPEC.md"], "HEAD")
    project = make_project(
        path=str(repo),
        tasks=tasks,
        spec_state_value=spec_state(["SPEC.md"], digest),
        focus_order=1,
    )
    projects = [project, *(extra_projects or [])]
    taskstore = FakeTaskStore(projects, agenda)
    conn = db.connect(config.state_dir)
    eligibility = EligibilityService(conn, taskstore, config, FakeProbe())
    return AgendaService(conn, taskstore, config, eligibility), taskstore, conn


def plan_for(*task_ids: str, deps: dict[str, list[str]] | None = None) -> str:
    lines = ["version: 1", "tasks:"]
    for task_id in task_ids:
        lines.append(f"  {task_id}:")
        lines.append("    gate:")
        lines.append('      - ["true"]')
        for dep in (deps or {}).get(task_id, []):
            lines.append(f"    depends_on: [{dep}]")
    return "\n".join(lines) + "\n"


def test_lock_unlock_and_snapshot(repo: Path, config: Config) -> None:
    day = date.today()
    agenda = make_agenda(
        day=day,
        focus=["p1"],
        items=[work_item()],
    )
    service, taskstore, conn = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One")],
        agenda=agenda,
    )
    with pytest.raises(TaskStoreError) as stale:
        service.lock(day, if_rev=99)
    assert stale.value.status_code == 409
    assert stale.value.payload == {"current_rev": 0}
    result = service.lock(day, if_rev=0)
    assert result.agenda.status == "locked"
    assert result.agenda.rev == 1
    snapshot = db.get_snapshot(conn, day.isoformat())
    assert snapshot is not None
    assert snapshot["rev"] == 1
    payload = service.snapshot(day)
    assert payload is not None
    assert payload["items"][0]["task"] == "t1"

    again = service.lock(day)
    assert again.already_locked
    assert len(taskstore.agenda_calls) == 1

    unlocked = service.unlock(day, "plans changed")
    assert unlocked.status == "draft"
    assert db.get_snapshot(conn, day.isoformat()) is None


def test_over_capacity(repo: Path, config: Config) -> None:
    day = date.today()
    agenda = make_agenda(
        day=day,
        focus=["p1"],
        items=[
            work_item(item_id="a_01", task="t1", order=0),
            work_item(item_id="a_02", task="t2", order=1),
        ],
        work_items=1,
    )
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1", "t2"),
        tasks=[
            TaskStoreTask(id="t1", title="One"),
            TaskStoreTask(id="t2", title="Two"),
        ],
        agenda=agenda,
    )
    with pytest.raises(AgendaValidationError) as caught:
        service.lock(day)
    assert any(issue.code == "over_capacity" for issue in caught.value.issues)


def test_dep_later_and_cycle(repo: Path, config: Config) -> None:
    day = date.today()
    agenda = make_agenda(
        day=day,
        focus=["p1"],
        items=[
            work_item(item_id="a_01", task="t2", order=0),
            work_item(item_id="a_02", task="t1", order=1),
        ],
    )
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1", "t2", deps={"t2": ["t1"]}),
        tasks=[
            TaskStoreTask(id="t1", title="One"),
            TaskStoreTask(id="t2", title="Two"),
        ],
        agenda=agenda,
    )
    with pytest.raises(AgendaValidationError) as caught:
        service.lock(day)
    assert any(issue.code == "dep_later" for issue in caught.value.issues)


def test_cycle_is_rejected(repo: Path, config: Config) -> None:
    day = date.today()
    agenda = make_agenda(
        day=day,
        focus=["p1"],
        items=[
            work_item(item_id="a_01", task="t1", order=0),
            work_item(item_id="a_02", task="t2", order=1),
        ],
    )
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1", "t2", deps={"t1": ["t2"], "t2": ["t1"]}),
        tasks=[
            TaskStoreTask(id="t1", title="One"),
            TaskStoreTask(id="t2", title="Two"),
        ],
        agenda=agenda,
    )
    with pytest.raises(AgendaValidationError) as caught:
        service.lock(day)
    assert any(issue.code == "cycle" for issue in caught.value.issues)


def test_manual_item_and_past_date(repo: Path, config: Config) -> None:
    day = date.today()
    chores = make_project(
        project_id="p2",
        name="Chores",
        tasks=[TaskStoreTask(id="t2", title="Chore")],
    )
    agenda = make_agenda(
        day=day,
        focus=["p1"],
        items=[
            work_item(item_id="a_01", task="t1", order=0),
            manual_item(item_id="a_02", project="p2", task="t2", order=1),
        ],
    )
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One")],
        agenda=agenda,
        extra_projects=[chores],
    )
    result = service.lock(day)
    assert result.agenda.status == "locked"

    past = day - timedelta(days=1)
    past_agenda = make_agenda(day=past, focus=["p1"], items=[work_item()])
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One")],
        agenda=past_agenda,
    )
    with pytest.raises(AgendaValidationError) as caught:
        service.lock(past)
    assert any(issue.code == "agenda_past" for issue in caught.value.issues)


def test_duplicate_and_focus_overflow(repo: Path, config: Config) -> None:
    day = date.today()
    agenda = make_agenda(
        day=day,
        focus=["p1", "p2", "p3", "p4"],
        items=[
            work_item(item_id="a_01", task="t1", order=0),
            work_item(item_id="a_02", task="t1", order=1),
        ],
    )
    projects = [
        make_project(project_id=f"p{index}") for index in (2, 3, 4)
    ]
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One")],
        agenda=agenda,
        extra_projects=projects,
    )
    with pytest.raises(AgendaValidationError) as caught:
        service.lock(day)
    codes = {issue.code for issue in caught.value.issues}
    assert "duplicate_task" in codes
    assert "focus_invalid" in codes


def test_lock_accepts_an_item_that_already_merged(
    repo: Path, config: Config
) -> None:
    day = date.today()
    item = TaskStoreAgendaItem(
        id="a_01",
        kind="manual",
        project="p1",
        objective="o_general",
        task="t1",
        order=0,
        status="done",
    )
    agenda = make_agenda(day=day, focus=["p1"], items=[item])
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One", status="done")],
        agenda=agenda,
    )
    result = service.lock(day, if_rev=0)
    assert result.agenda.status == "locked"


def test_lock_still_refuses_a_task_in_progress(
    repo: Path, config: Config
) -> None:
    day = date.today()
    item = TaskStoreAgendaItem(
        id="a_01",
        kind="manual",
        project="p1",
        objective="o_general",
        task="t1",
        order=0,
        status="planned",
    )
    agenda = make_agenda(day=day, focus=["p1"], items=[item])
    service, _, _ = setup_agenda(
        repo,
        config,
        plan=plan_for("t1"),
        tasks=[TaskStoreTask(id="t1", title="One", status="doing")],
        agenda=agenda,
    )
    with pytest.raises(AgendaValidationError) as failed:
        service.lock(day, if_rev=0)
    assert any(issue.code == "task_not_open" for issue in failed.value.issues)
