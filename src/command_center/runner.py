from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import db, gitutil
from . import policy as policy_module
from .agent import OpenCodeAgent
from .config import Config
from .eligibility import EligibilityService, task_status_map
from .errors import RunError, TaskStoreError
from .events import EventBus
from .gate import GateResult, run_gate
from .paths import project_root
from .plan import load_plan
from .probe import ModelProbe
from .prompt import build_coder_prompt, build_reviewer_prompt
from .review import ReviewResult, ReviewVerdict, parse_review
from .runs import RunRecord, RunState
from .taskstore import TaskStoreClient, TaskStoreObjective, TaskStoreProject, TaskStoreTask


def _review_note(prefix: str, result: ReviewResult) -> str:
    lines = [f"reviewer {prefix}: " + "; ".join(result.reasons)]
    for finding in result.findings:
        location = finding.file or "?"
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        lines.append(f"  {finding.severity} {location}: {finding.note}")
    return "\n".join(lines)

GateRunner = Callable[..., GateResult]
PolicyScan = Callable[..., policy_module.PolicyResult]


def check_target_agents(root: Path) -> None:
    """Refuse a repo that tries to shadow the Command Center agents."""
    for name in (
        "opencode.json",
        "opencode.jsonc",
        ".opencode/opencode.json",
        ".opencode/opencode.jsonc",
    ):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r'"cc-[a-z0-9-]+"\s*:', text):
            raise RunError(f"{name} defines a cc-* agent; refusing to run")
    agents = root / ".opencode" / "agents"
    if agents.is_dir():
        shadow = sorted(path.name for path in agents.glob("cc-*.md"))
        if shadow:
            raise RunError(
                f".opencode/agents defines {', '.join(shadow)}; refusing to run"
            )


class RunEngine:
    def __init__(
        self,
        config: Config,
        taskstore: TaskStoreClient,
        *,
        bus: EventBus | None = None,
        agent: OpenCodeAgent | None = None,
        gate_runner: GateRunner | None = None,
        policy_scan: PolicyScan | None = None,
    ):
        self.config = config
        self.taskstore = taskstore
        self.bus = bus or EventBus()
        self.agent = agent or OpenCodeAgent(config)
        self.gate_runner = gate_runner or run_gate
        self.policy_scan = policy_scan or policy_module.scan

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.config.state_dir)

    def _find(
        self, task_id: str
    ) -> tuple[TaskStoreProject, TaskStoreObjective, TaskStoreTask]:
        state = self.taskstore.state()
        for project in state.projects:
            for objective in project.objectives:
                for task in objective.tasks:
                    if task.id == task_id:
                        return project, objective, task
        raise RunError(f"task {task_id!r} not found")

    def _model(self, model_id: str | None) -> tuple[str, Any]:
        selected = model_id or self.config.roles.get("coder")
        if not selected:
            raise RunError("no coder model configured")
        model = self.config.models.get(selected)
        if model is None:
            raise RunError(f"unknown model {selected!r}")
        status = ModelProbe(self.config).check(selected)
        if status is None or not status.up:
            reason = status.reason if status else "model unavailable"
            raise RunError(f"model {selected} is down: {reason}")
        return selected, model

    def _agenda_item(self, task_id: str) -> str | None:
        try:
            agenda = self.taskstore.get_agenda(date.today())
        except TaskStoreError:
            return None
        if agenda is None or agenda.status != "locked":
            return None
        for item in agenda.items:
            if item.task == task_id and item.status == "planned":
                return item.id
        return None

    def _reviewer(self, coder_id: str, reviewer_id: str | None = None):
        reviewer_id = reviewer_id or self.config.roles.get("reviewer")
        if not reviewer_id:
            raise RunError("no reviewer model configured")
        model = self.config.models.get(reviewer_id)
        if model is None:
            raise RunError(f"unknown reviewer model {reviewer_id!r}")
        status = ModelProbe(self.config).check(reviewer_id)
        if status is None or not status.up:
            reason = status.reason if status else "reviewer model unavailable"
            raise RunError(f"reviewer model {reviewer_id} is down: {reason}")
        return reviewer_id, model

    def create(
        self,
        task_id: str,
        model_id: str | None = None,
        reviewer_id: str | None = None,
    ) -> RunRecord:
        project, objective, task = self._find(task_id)
        root = project_root(project.path)
        if root is not None:
            check_target_agents(root)
        selected, model = self._model(model_id)
        reviewer_id, reviewer = self._reviewer(selected, reviewer_id)
        conn = self._conn()
        try:
            active = db.active_run_for_project(conn, project.id)
            if active:
                raise RunError(
                    f"project {project.id} already has an active run {active['id']}"
                )
            shape = db.active_shape_for_project(conn, project.id)
            if shape:
                raise RunError(
                    f"project {project.id} has an active shape {shape['id']}"
                )
            if db.lock_exists(conn, task.id):
                raise RunError(f"task {task.id} is locked")
            state = self.taskstore.state()
            statuses = task_status_map(state.projects)
            eligibility = EligibilityService(conn, self.taskstore, self.config)
            check = eligibility.check_task_for(
                project, objective, task, statuses
            )
            if not check.ready:
                codes = ", ".join(reason.code for reason in check.reasons)
                raise RunError(f"task is not eligible: {codes}")
            record = RunRecord.new(
                project=project.id,
                objective=objective.id,
                task=task.id,
                model_requested=selected,
                model_resolved=model.model,
                provider=selected,
                reviewer_model=reviewer_id,
                reviewer_resolved=reviewer.model,
            )
            record.branch = f"cc/{task.id}-{record.id}"
            record.run_dir = str(self.config.state_dir / "runs" / record.id)
            record.agenda_item = self._agenda_item(task.id)
            record.save(conn)
            return record
        finally:
            conn.close()

    def execute(self, record: RunRecord) -> RunRecord:
        conn = self._conn()
        claimed = False
        task_rev: int | None = None
        project: TaskStoreProject | None = None
        objective: TaskStoreObjective | None = None
        task: TaskStoreTask | None = None
        root: Path | None = None
        worktree: Path | None = None
        try:
            project, objective, task = self._find(record.task)
            state = self.taskstore.state()
            statuses = task_status_map(state.projects)
            eligibility = EligibilityService(conn, self.taskstore, self.config)
            check = eligibility.check_task_for(project, objective, task, statuses)
            if not check.ready:
                codes = ", ".join(reason.code for reason in check.reasons)
                raise RunError(f"task is not eligible: {codes}")
            if not db.acquire_lock(conn, task.id, record.id):
                raise RunError(f"task {task.id} is locked")
            claimed_task = self.taskstore.patch_task(
                project.id,
                objective.id,
                task.id,
                {"status": "doing", "if_rev": task.rev},
            )
            claimed = True
            task_rev = claimed_task.rev
            self._agenda_status(conn, record, "running")
            record.state = RunState.running
            record.started_at = datetime.now(UTC)
            record.attempt = 1
            record.save(conn)
            self._publish(
                conn,
                "run.started",
                {
                    "run_id": record.id,
                    "project": project.id,
                    "task": task.id,
                    "model": record.model_resolved,
                },
            )

            root = project_root(project.path)
            if root is None:
                raise RunError("project has no path")
            check_target_agents(root)
            if not gitutil.is_git_repo(root):
                raise RunError(f"{root} is not a git repository")
            base = gitutil.ensure_integration(root)
            record.base_sha = base
            worktree = self.config.worktree_root / project.id / f"item-{task.id}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            gitutil.worktree_remove(root, worktree)
            gitutil.worktree_add(root, worktree, record.branch, base)
            record.worktree = str(worktree)
            self._install_hook(root, worktree, record)
            record.save(conn)

            plan = load_plan(worktree)
            gate = plan.gate_for(objective.id, task.id)
            if not gate:
                raise RunError("task resolves to an empty gate from integration")
            scope = plan.scope_for(task.id)
            run_dir = Path(record.run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            notes = list(record.notes)
            brief = self._item_brief(conn, task.id, exclude=record.id)
            thread = db.get_work_session(conn, project.id, task.id) or {}
            author_session = thread.get("author_session")
            critic_session = thread.get("critic_session")

            for attempt in range(1, self.config.budgets.max_attempts + 1):
                if db.cancel_requested(conn, record.id):
                    return self._cancel(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        root,
                        worktree,
                    )
                record.attempt = attempt
                record.state = RunState.running
                record.notes = notes
                record.save(conn)
                gitutil.reset_hard(worktree, base)
                gitutil.clean(worktree)
                prompt = build_coder_prompt(
                    record=record,
                    project=project,
                    objective=objective,
                    task=task,
                    plan=plan,
                    scope=scope,
                    previous_notes=notes,
                    item_brief=brief,
                    resumed=bool(author_session),
                )
                (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                agent_result = self.agent.run(
                    prompt,
                    cwd=worktree,
                    run_dir=run_dir,
                    model=record.model_resolved,
                    session_id=author_session,
                )
                author_session = agent_result.session_id or author_session
                db.save_work_session(
                    conn,
                    project.id,
                    task.id,
                    author_session=author_session,
                    critic_session=critic_session,
                )
                record.session_id = author_session
                if agent_result.exit_code != 0:
                    notes.append(f"agent exited {agent_result.exit_code}")
                record.save(conn)
                if db.cancel_requested(conn, record.id):
                    return self._cancel(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        root,
                        worktree,
                    )

                gitutil.add_all(worktree)
                diff_text = gitutil.staged_diff(worktree, base)
                diff_path = run_dir / "diff.patch"
                diff_path.write_text(diff_text, encoding="utf-8")
                record.diff_path = str(diff_path)
                record.save(conn)
                if not diff_text.strip():
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "no_diff",
                        "the coder produced no diff",
                        worktree,
                    )

                gate_result = self.gate_runner(
                    worktree,
                    gate,
                    timeout=self.config.budgets.gate_timeout_seconds,
                    gate_path=run_dir / "gate.json",
                )
                record.gate_path = str(run_dir / "gate.json")
                record.save(conn)
                if not gate_result.passed:
                    notes.append(self._gate_note(gate_result))
                    record.notes = notes
                    record.state = RunState.gate_failed
                    record.save(conn)
                    self._publish(
                        conn,
                        "gate.failed",
                        {
                            "run_id": record.id,
                            "command": gate_result.commands[-1].argv
                            if gate_result.commands
                            else [],
                            "exit_code": gate_result.commands[-1].exit_code
                            if gate_result.commands
                            else None,
                        },
                    )
                    if attempt < self.config.budgets.max_attempts:
                        continue
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "gate_failed",
                        self._gate_note(gate_result),
                        worktree,
                    )

                gitutil.add_all(worktree)
                after_gate = gitutil.staged_diff(worktree, base)
                if after_gate != diff_text:
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "gate_dirty",
                        "the gate modified tracked or non-ignored files",
                        worktree,
                    )

                policy_result = self.policy_scan(
                    worktree,
                    base,
                    scope=scope,
                    config=self.config.policy,
                )
                policy_path = run_dir / "policy.json"
                policy_path.write_text(
                    json.dumps(
                        policy_result.to_dict(), indent=2, ensure_ascii=False
                    ),
                    encoding="utf-8",
                )
                record.policy_path = str(policy_path)
                record.policy_passed = policy_result.passed
                record.save(conn)
                if not policy_result.passed:
                    codes = ", ".join(
                        finding.code for finding in policy_result.findings
                    )
                    notes.append(f"policy failed: {codes}")
                    record.notes = notes
                    record.state = RunState.policy_failed
                    record.save(conn)
                    self._publish(
                        conn,
                        "policy.failed",
                        {"run_id": record.id, "codes": codes},
                    )
                    if attempt < self.config.budgets.max_attempts:
                        continue
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "policy_failed",
                        f"policy failed: {codes}",
                        worktree,
                    )

                review_prompt = build_reviewer_prompt(
                    record=record,
                    project=project,
                    objective=objective,
                    task=task,
                    plan=plan,
                    scope=scope,
                    item_brief=brief,
                    resumed=bool(critic_session),
                )
                (run_dir / "review-prompt.txt").write_text(
                    review_prompt, encoding="utf-8"
                )
                review_agent = self.agent.run(
                    review_prompt,
                    cwd=worktree,
                    run_dir=run_dir,
                    model=record.reviewer_resolved,
                    agent="cc-reviewer",
                    append=True,
                    session_id=critic_session,
                )
                critic_session = review_agent.session_id or critic_session
                db.save_work_session(
                    conn,
                    project.id,
                    task.id,
                    author_session=author_session,
                    critic_session=critic_session,
                )
                record.reviewer_session_id = critic_session
                if review_agent.exit_code != 0:
                    review_result = ReviewResult(
                        verdict=ReviewVerdict.changes,
                        reasons=[f"reviewer exited {review_agent.exit_code}"],
                        findings=[],
                    )
                else:
                    review_result = parse_review(review_agent.final_text)
                review_path = run_dir / "review.json"
                review_path.write_text(
                    json.dumps(
                        review_result.model_dump(mode="json"),
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                record.reviewer_path = str(review_path)
                record.reviewer_verdict = review_result.verdict.value
                record.save(conn)
                self._publish(
                    conn,
                    "review.verdict",
                    {"run_id": record.id, "verdict": review_result.verdict.value},
                )
                if review_result.verdict == ReviewVerdict.block:
                    detail = _review_note("blocked", review_result)
                    notes.append(detail)
                    record.notes = notes
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "review_block",
                        detail,
                        worktree,
                    )
                if review_result.verdict == ReviewVerdict.changes:
                    detail = _review_note("changes", review_result)
                    notes.append(detail)
                    record.notes = notes
                    record.state = RunState.review_changes
                    record.save(conn)
                    if attempt < self.config.budgets.max_attempts:
                        continue
                    return self._block(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        "review_changes",
                        detail,
                        worktree,
                    )

                if db.cancel_requested(conn, record.id):
                    return self._cancel(
                        conn,
                        record,
                        project,
                        objective,
                        task,
                        task_rev,
                        root,
                        worktree,
                    )

                record.state = RunState.merging
                record.save(conn)
                commit_sha = gitutil.commit(
                    worktree, f"cc: {task.title} ({record.id})"
                )
                if gitutil.head_commit(root, "cc/integration") != base:
                    raise RunError("cc/integration moved during the run")
                gitutil.move_branch(root, "cc/integration", commit_sha)
                record.merge_sha = commit_sha
                last_run = {
                    "id": record.id,
                    "verdict": "merged",
                    "at": db.now_iso(),
                }
                try:
                    self.taskstore.patch_task(
                        project.id,
                        objective.id,
                        task.id,
                        {
                            "status": "done",
                            "last_run": last_run,
                            "if_rev": task_rev,
                        },
                    )
                except TaskStoreError as exc:
                    notes.append(f"task completion failed: {exc}")
                self._agenda_status(conn, record, "done")
                record.state = RunState.merged
                record.verdict = "merged"
                record.policy_passed = True
                record.finished_at = datetime.now(UTC)
                record.notes = notes
                record.save(conn)
                self._publish(
                    conn,
                    "run.merged",
                    {"run_id": record.id, "merge_sha": commit_sha},
                )
                gitutil.worktree_remove(root, worktree)
                db.release_lock(conn, task.id, record.id)
                db.clear_work_session(conn, project.id, task.id)
                return record

            return self._block(
                conn,
                record,
                project,
                objective,
                task,
                task_rev,
                "exhausted",
                "attempt budget exhausted",
                worktree,
            )
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            if claimed and project and objective and task and task_rev is not None:
                try:
                    self.taskstore.patch_task(
                        project.id,
                        objective.id,
                        task.id,
                        {
                            "status": "blocked",
                            "last_run": {
                                "id": record.id,
                                "verdict": "error",
                                "at": db.now_iso(),
                            },
                            "if_rev": task_rev,
                        },
                    )
                except TaskStoreError:
                    pass
                self._agenda_status(conn, record, "review")
            record.state = RunState.error
            record.error = str(exc)
            record.finished_at = datetime.now(UTC)
            record.save(conn)
            db.release_lock(conn, record.task, record.id)
            self._publish(
                conn,
                "run.finished",
                {"run_id": record.id, "state": "error", "verdict": None},
            )
            return record
        finally:
            conn.close()

    def _item_brief(
        self, conn: sqlite3.Connection, task_id: str, *, exclude: str
    ) -> str:
        """A compact history of this item, so memory does not live only in a session."""
        lines: list[str] = []
        for row in db.recent_runs_for_task(conn, task_id):
            if row.get("id") == exclude:
                continue
            try:
                prior = RunRecord.model_validate(row)
            except Exception:  # noqa: BLE001 - a bad row must not fail a run
                continue
            line = f"  - {prior.id} attempt {prior.attempt}: {prior.state}"
            if prior.verdict and prior.verdict != str(prior.state):
                line += f" ({prior.verdict})"
            if prior.reviewer_verdict:
                line += f", review {prior.reviewer_verdict}"
            if prior.merge_sha:
                line += f", merged {prior.merge_sha[:12]}"
            notes = [note for note in prior.notes if note]
            if notes:
                line += " — " + "; ".join(notes)[:400]
            lines.append(line)
        return "\n".join(lines)

    def _install_hook(self, root: Path, worktree: Path, record: RunRecord) -> None:
        hooks = self.config.state_dir / "hooks" / record.id
        hooks.mkdir(parents=True, exist_ok=True)
        pre_push = hooks / "pre-push"
        pre_push.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        pre_push.chmod(0o755)
        gitutil.worktree_config(root, worktree, "core.hooksPath", str(hooks))

    def _agenda_status(
        self, conn: sqlite3.Connection, record: RunRecord, status: str
    ) -> None:
        if not record.agenda_item:
            return
        try:
            agenda = self.taskstore.get_agenda(date.today())
            if agenda is None:
                return
            item = next(
                (item for item in agenda.items if item.id == record.agenda_item),
                None,
            )
            if item is None:
                return
            self.taskstore.patch_agenda_item(
                agenda.date,
                item.id,
                {
                    "status": status,
                    "run": record.id if status in ("running", "done") else None,
                    "if_rev": agenda.rev,
                },
            )
        except TaskStoreError as exc:
            record.notes.append(f"agenda item update failed: {exc}")

    def _block(
        self,
        conn: sqlite3.Connection,
        record: RunRecord,
        project: TaskStoreProject,
        objective: TaskStoreObjective,
        task: TaskStoreTask,
        task_rev: int | None,
        code: str,
        detail: str,
        worktree: Path | None,
    ) -> RunRecord:
        notes = list(record.notes)
        notes.append(detail)
        if task_rev is not None:
            try:
                self.taskstore.patch_task(
                    project.id,
                    objective.id,
                    task.id,
                    {
                        "status": "blocked",
                        "last_run": {
                            "id": record.id,
                            "verdict": code,
                            "at": db.now_iso(),
                        },
                        "if_rev": task_rev,
                    },
                )
            except TaskStoreError as exc:
                notes.append(f"task block failed: {exc}")
        self._agenda_status(conn, record, "review")
        record.state = RunState.blocked
        record.verdict = code
        record.finished_at = datetime.now(UTC)
        record.notes = notes
        record.save(conn)
        db.release_lock(conn, task.id, record.id)
        self._publish(
            conn, "run.blocked", {"run_id": record.id, "code": code, "detail": detail}
        )
        return record

    def _cancel(
        self,
        conn: sqlite3.Connection,
        record: RunRecord,
        project: TaskStoreProject,
        objective: TaskStoreObjective,
        task: TaskStoreTask,
        task_rev: int | None,
        root: Path | None,
        worktree: Path | None,
    ) -> RunRecord:
        if task_rev is not None:
            try:
                self.taskstore.patch_task(
                    project.id,
                    objective.id,
                    task.id,
                    {"status": "open", "if_rev": task_rev},
                )
            except TaskStoreError as exc:
                record.notes.append(f"task reopen failed: {exc}")
        self._agenda_status(conn, record, "planned")
        record.state = RunState.cancelled
        record.verdict = "cancelled"
        record.finished_at = datetime.now(UTC)
        record.save(conn)
        db.release_lock(conn, task.id, record.id)
        if root is not None and worktree is not None:
            gitutil.worktree_remove(root, worktree)
        self._publish(
            conn,
            "run.finished",
            {
                "run_id": record.id,
                "state": "cancelled",
                "verdict": "cancelled",
            },
        )
        return record

    def _publish(
        self, conn: sqlite3.Connection, event: str, data: dict[str, Any]
    ) -> None:
        self.bus.publish(event, data)
        db.record_event(conn, event, data)

    @staticmethod
    def _gate_note(result: GateResult) -> str:
        if not result.commands:
            return "gate had no commands"
        last = result.commands[-1]
        return f"gate failed: {' '.join(last.argv)} (exit {last.exit_code})"
