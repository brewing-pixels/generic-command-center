from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from helpers import (
    FakeAgent,
    FakeTaskStore,
    commit,
    make_agenda,
    make_project,
    spec_state,
    work_item,
)

from command_center import db, gitutil, spec
from command_center.config import Config
from command_center.errors import RunError
from command_center.events import EventBus
from command_center.runner import RunEngine
from command_center.taskstore import TaskStoreTask


def make_config(config: Config, attempts: int = 1) -> Config:
    return config.model_copy(
        update={
            "budgets": config.budgets.model_copy(
                update={"max_attempts": attempts}
            )
        }
    )


def setup(
    repo: Path,
    config: Config,
    *,
    gate: str = '["true"]',
    scope: str = "src/**",
    agent: FakeAgent | None = None,
    agenda=None,
    attempts: int = 1,
):
    (repo / "command-center.yaml").write_text(
        "version: 1\n"
        "tasks:\n"
        "  t1:\n"
        "    gate:\n"
        f"      - {gate}\n"
        "    scope:\n"
        f"      - \"{scope}\"\n",
        encoding="utf-8",
    )
    commit(repo, "plan")
    digest = spec.compute_digest(repo, ["SPEC.md"], "HEAD")
    project = make_project(
        path=str(repo),
        tasks=[TaskStoreTask(id="t1", title="One")],
        spec_state_value=spec_state(["SPEC.md"], digest),
    )
    taskstore = FakeTaskStore([project], agenda)
    engine = RunEngine(
        make_config(config, attempts),
        taskstore,
        bus=EventBus(),
        agent=agent or FakeAgent(),
    )
    return engine, taskstore


def test_a_second_run_resumes_the_item_thread(
    repo: Path, config: Config
) -> None:
    agent = FakeAgent(review_verdict="changes")
    engine, taskstore = setup(repo, config, agent=agent, attempts=1)
    first = engine.execute(engine.create("t1"))
    assert first.state == "blocked"
    assert Path(first.worktree).name == "item-t1"

    taskstore.patch_task(
        "p1",
        "o_general",
        "t1",
        {
            "status": "open",
            "if_rev": taskstore.projects["p1"].objectives[0].tasks[0].rev,
        },
    )
    second = engine.execute(engine.create("t1"))

    assert Path(second.worktree) == Path(first.worktree)
    assert agent.sessions == [None, None, "ses_test", "ses_review"]
    assert "Prior work on this item" in agent.prompts[2]
    assert f"{first.id} attempt 1" in agent.prompts[2]
    assert "this session worked on an earlier attempt" in agent.prompts[2].lower()

    conn = db.connect(config.state_dir)
    try:
        thread = db.get_work_session(conn, "p1", "t1")
    finally:
        conn.close()
    assert thread is not None
    assert thread["author_session"] == "ses_test"
    assert thread["critic_session"] == "ses_review"


def test_a_stale_item_directory_does_not_block_the_run(
    repo: Path, config: Config
) -> None:
    stale = config.worktree_root / "p1" / "item-t1"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("left over", encoding="utf-8")
    engine, _ = setup(repo, config)
    result = engine.execute(engine.create("t1"))
    assert result.state == "merged"
    assert not Path(result.worktree).exists()


def test_merged_run_retires_the_item_thread(
    repo: Path, config: Config
) -> None:
    engine, _ = setup(repo, config)
    result = engine.execute(engine.create("t1"))
    assert result.state == "merged"
    conn = db.connect(config.state_dir)
    try:
        assert db.get_work_session(conn, "p1", "t1") is None
    finally:
        conn.close()


def test_run_merges_into_integration(repo: Path, config: Config) -> None:
    engine, taskstore = setup(repo, config)
    master = gitutil.head_commit(repo, "HEAD")
    record = engine.create("t1")
    result = engine.execute(record)
    assert result.state == "merged"
    assert result.merge_sha
    assert result.merge_sha != master
    assert gitutil.head_commit(repo, "cc/integration") == result.merge_sha
    assert gitutil.head_commit(repo, "HEAD") == master
    assert taskstore.projects["p1"].objectives[0].tasks[0].status == "done"
    assert not Path(result.worktree).exists()
    conn = db.connect(config.state_dir)
    try:
        stored = db.get_run(conn, result.id)
    finally:
        conn.close()
    assert stored is not None
    assert stored["state"] == "merged"
    assert stored["policy_passed"] is True


def test_gate_failure_blocks_without_merge(repo: Path, config: Config) -> None:
    engine, taskstore = setup(repo, config, gate='["false"]')
    master = gitutil.head_commit(repo, "HEAD")
    result = engine.execute(engine.create("t1"))
    assert result.state == "blocked"
    assert result.verdict == "gate_failed"
    assert gitutil.head_commit(repo, "cc/integration") == master
    assert taskstore.projects["p1"].objectives[0].tasks[0].status == "blocked"


def test_policy_failure_blocks(repo: Path, config: Config) -> None:
    engine, _ = setup(repo, config, agent=FakeAgent(path=".env", content="A=1\n"))
    result = engine.execute(engine.create("t1"))
    assert result.state == "blocked"
    assert result.verdict == "policy_failed"
    assert result.policy_passed is False


def test_no_diff_blocks(repo: Path, config: Config) -> None:
    engine, _ = setup(repo, config, agent=FakeAgent(path=None))
    result = engine.execute(engine.create("t1"))
    assert result.state == "blocked"
    assert result.verdict == "no_diff"


def test_agenda_item_moves_running_then_done(repo: Path, config: Config) -> None:
    agenda = make_agenda(
        day=date.today(),
        focus=["p1"],
        items=[work_item()],
    )
    agenda = agenda.model_copy(update={"status": "locked", "rev": 1})
    engine, taskstore = setup(repo, config, agenda=agenda)
    result = engine.execute(engine.create("t1"))
    assert result.state == "merged"
    statuses = [call[1].get("status") for call in taskstore.agenda_item_calls]
    assert statuses == ["running", "done"]


def test_refuses_shadow_agents(repo: Path, config: Config) -> None:
    engine, _ = setup(repo, config)
    agents = repo / ".opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "cc-coder.md").write_text("shadow\n", encoding="utf-8")
    with pytest.raises(RunError):
        engine.create("t1")


def test_reviewer_may_use_the_coder_model(
    repo: Path, config: Config
) -> None:
    engine, _ = setup(repo, config)
    cfg = config.model_copy(deep=True)
    cfg.roles = {"coder": "hosted", "reviewer": "hosted"}
    engine.config = cfg
    record = engine.create("t1")
    assert record.reviewer_model == "hosted"


def test_reviewer_approve_is_recorded(repo: Path, config: Config) -> None:
    agent = FakeAgent()
    engine, _ = setup(repo, config, agent=agent)
    result = engine.execute(engine.create("t1"))
    assert result.state == "merged"
    assert result.reviewer_verdict == "approve"
    assert result.reviewer_model == "hosted"
    assert result.reviewer_resolved == "provider/model-name"
    assert result.reviewer_path
    assert Path(result.reviewer_path).is_file()
    assert agent.calls == ["cc-coder", "cc-reviewer"]


def test_reviewer_block_stops_without_retry(repo: Path, config: Config) -> None:
    agent = FakeAgent(review_verdict="block")
    engine, _ = setup(repo, config, agent=agent, attempts=3)
    master = gitutil.head_commit(repo, "HEAD")
    result = engine.execute(engine.create("t1"))
    assert result.state == "blocked"
    assert result.verdict == "review_block"
    assert gitutil.head_commit(repo, "cc/integration") == master
    assert agent.calls == ["cc-coder", "cc-reviewer"]


def test_reviewer_changes_consumes_the_budget(repo: Path, config: Config) -> None:
    agent = FakeAgent(review_verdict="changes")
    engine, taskstore = setup(repo, config, agent=agent, attempts=2)
    result = engine.execute(engine.create("t1"))
    assert result.state == "blocked"
    assert result.verdict == "review_changes"
    assert agent.calls == [
        "cc-coder",
        "cc-reviewer",
        "cc-coder",
        "cc-reviewer",
    ]
    assert taskstore.projects["p1"].objectives[0].tasks[0].status == "blocked"


