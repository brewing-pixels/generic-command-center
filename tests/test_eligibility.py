from __future__ import annotations

from pathlib import Path

from helpers import FakeProbe, FakeTaskStore, commit, make_project, spec_state

from command_center import db, spec
from command_center.config import Config
from command_center.eligibility import RC, EligibilityService
from command_center.taskstore import TaskStoreTask


def plan_file(repo: Path, text: str) -> None:
    (repo / "command-center.yaml").write_text(text, encoding="utf-8")
    commit(repo, "plan")


def service_for(
    repo: Path,
    config: Config,
    *,
    plan: str,
    tasks: list[TaskStoreTask],
    with_spec: bool = True,
    probe_up: bool = True,
    status: str = "ongoing",
) -> tuple[EligibilityService, FakeTaskStore, str | None]:
    plan_file(repo, plan)
    digest = None
    if with_spec:
        digest = spec.compute_digest(repo, ["SPEC.md"], "HEAD")
    project = make_project(
        path=str(repo),
        status=status,
        tasks=tasks,
        spec_state_value=spec_state(["SPEC.md"], digest) if digest else None,
    )
    taskstore = FakeTaskStore([project])
    conn = db.connect(config.state_dir)
    eligibility = EligibilityService(
        conn, taskstore, config, probe=FakeProbe(up=probe_up)
    )
    return eligibility, taskstore, digest


def test_ready_task(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="""
version: 1
tasks:
  t1:
    gate:
      - ["true"]
""",
        tasks=[TaskStoreTask(id="t1", title="One")],
    )
    checks = eligibility.check_all()
    assert len(checks) == 1
    assert checks[0].result.ready
    assert checks[0].result.reasons == []


def test_spec_unapproved_and_dirty(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="version: 1\ntasks:\n  t1:\n    gate:\n      - [\"true\"]\n",
        tasks=[TaskStoreTask(id="t1", title="One")],
        with_spec=False,
    )
    result = eligibility.check_all()[0].result
    assert not result.ready
    assert any(reason.code == RC.SPEC_UNAPPROVED for reason in result.reasons)

    eligibility, taskstore, digest = service_for(
        repo,
        config,
        plan="version: 1\ntasks:\n  t1:\n    gate:\n      - [\"true\"]\n",
        tasks=[TaskStoreTask(id="t1", title="One")],
    )
    (repo / "SPEC.md").write_text("changed\n", encoding="utf-8")
    commit(repo, "change spec")
    fresh = EligibilityService(
        db.connect(config.state_dir), taskstore, config, FakeProbe()
    )
    result = fresh.check_all()[0].result
    assert any(reason.code == RC.SPEC_DIRTY for reason in result.reasons)


def test_base_dirty(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="version: 1\ntasks:\n  t1:\n    gate:\n      - [\"true\"]\n",
        tasks=[TaskStoreTask(id="t1", title="One")],
    )
    (repo / "SPEC.md").write_text("dirty\n", encoding="utf-8")
    fresh = EligibilityService(
        db.connect(config.state_dir),
        eligibility.taskstore,
        config,
        FakeProbe(),
    )
    result = fresh.check_all()[0].result
    assert any(reason.code == RC.BASE_DIRTY for reason in result.reasons)


def test_gate_and_dependencies(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="""
version: 1
tasks:
  t1:
    gate:
      - ["true"]
  t2:
    gate:
      - ["true"]
    depends_on: [t1]
""",
        tasks=[
            TaskStoreTask(id="t1", title="One"),
            TaskStoreTask(id="t2", title="Two"),
        ],
    )
    checks = {check.task.id: check for check in eligibility.check_all()}
    assert checks["t1"].result.ready
    assert any(
        reason.code == RC.DEP_MISSING for reason in checks["t2"].result.reasons
    )

    project = eligibility.taskstore.projects["p1"]
    objective = project.objectives[0]
    t2 = next(task for task in objective.tasks if task.id == "t2")
    later = eligibility.check_task_for(
        project,
        objective,
        t2,
        {"t1": "open", "t2": "open"},
        agenda_order={"t1": 1, "t2": 0},
    )
    assert any(reason.code == RC.DEP_LATER for reason in later.reasons)
    earlier = eligibility.check_task_for(
        project,
        objective,
        t2,
        {"t1": "open", "t2": "open"},
        agenda_order={"t1": 0, "t2": 1},
    )
    assert earlier.ready


def test_no_gate_and_closed_status(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="version: 1\n",
        tasks=[TaskStoreTask(id="t1", title="One")],
        with_spec=False,
    )
    result = eligibility.check_all()[0].result
    assert any(reason.code == RC.NO_GATE for reason in result.reasons)

    eligibility, _, _ = service_for(
        repo,
        config,
        plan="version: 1\ntasks:\n  t1:\n    gate:\n      - [\"true\"]\n",
        tasks=[TaskStoreTask(id="t1", title="One", status="done")],
    )
    result = eligibility.check_all()[0].result
    assert any(reason.code == RC.TASK_NOT_OPEN for reason in result.reasons)


def test_missing_and_not_git(tmp_path: Path, config: Config) -> None:
    project = make_project(path=str(tmp_path / "missing"))
    taskstore = FakeTaskStore([project])
    eligibility = EligibilityService(
        db.connect(config.state_dir), taskstore, config, FakeProbe()
    )
    result = eligibility.check_all()[0].result
    assert any(reason.code == RC.PROJECT_MISSING for reason in result.reasons)

    plain = tmp_path / "plain"
    plain.mkdir()
    project = make_project(path=str(plain))
    taskstore = FakeTaskStore([project])
    eligibility = EligibilityService(
        db.connect(config.state_dir), taskstore, config, FakeProbe()
    )
    result = eligibility.check_all()[0].result
    assert any(reason.code == RC.NOT_GIT for reason in result.reasons)


def test_model_down_is_a_warning(repo: Path, config: Config) -> None:
    eligibility, _, _ = service_for(
        repo,
        config,
        plan="version: 1\ntasks:\n  t1:\n    gate:\n      - [\"true\"]\n",
        tasks=[TaskStoreTask(id="t1", title="One")],
        probe_up=False,
    )
    result = eligibility.check_all()[0].result
    assert result.ready
    assert any(warning.code == RC.MODEL_DOWN for warning in result.warnings)
