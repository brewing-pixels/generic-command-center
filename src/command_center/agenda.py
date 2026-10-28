from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from . import db
from .config import Config
from .eligibility import RC, EligibilityService, Reason, task_status_map
from .errors import AgendaError, TaskStoreError
from .taskstore import TaskStoreAgenda, TaskStoreClient


@dataclass(frozen=True)
class AgendaIssue:
    item_id: str | None
    code: str
    detail: str


class AgendaValidationError(AgendaError):
    def __init__(self, issues: list[AgendaIssue]):
        self.issues = issues
        super().__init__(f"{len(issues)} agenda issue(s)")


@dataclass
class LockResult:
    agenda: TaskStoreAgenda
    warnings: list[Reason] = field(default_factory=list)
    already_locked: bool = False


def _cycles(items: list[dict[str, str]], deps: dict[str, list[str]]) -> set[str]:
    graph: dict[str, list[str]] = {}
    selected = {item["task"] for item in items}
    for item in items:
        graph[item["task"]] = [
            dep for dep in deps.get(item["task"], []) if dep in selected
        ]
    visiting: set[str] = set()
    visited: set[str] = set()
    found: set[str] = set()

    def walk(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            start = stack.index(node)
            found.update(stack[start:])
            return
        visiting.add(node)
        stack.append(node)
        for dep in graph.get(node, []):
            walk(dep, stack)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        walk(node, [])
    return found


class AgendaService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        taskstore: TaskStoreClient,
        config: Config,
        eligibility: EligibilityService,
    ):
        self.conn = conn
        self.taskstore = taskstore
        self.config = config
        self.eligibility = eligibility

    def show(self, day: date) -> TaskStoreAgenda | None:
        return self.taskstore.get_agenda(day)

    def suggest(self, day: date, replace: bool = False) -> TaskStoreAgenda:
        return self.taskstore.suggest_agenda(day, replace=replace)

    def snapshot(self, day: date) -> dict[str, object] | None:
        row = db.get_snapshot(self.conn, day.isoformat())
        return json.loads(row["payload"]) if row else None

    def lock(self, day: date, if_rev: int | None = None) -> LockResult:
        agenda = self.taskstore.get_agenda(day)
        if agenda is None:
            raise AgendaError(f"no agenda for {day.isoformat()}")
        if if_rev is not None and agenda.rev != if_rev:
            raise TaskStoreError(
                "agenda revision changed",
                409,
                {"current_rev": agenda.rev},
            )
        if agenda.status == "locked":
            snapshot = db.get_snapshot(self.conn, day.isoformat())
            if snapshot and snapshot["rev"] == agenda.rev:
                return LockResult(agenda, already_locked=True)
            if snapshot is None:
                self._store_snapshot(agenda)
                return LockResult(agenda, already_locked=True)
            raise AgendaError("locked agenda revision does not match the snapshot")

        issues: list[AgendaIssue] = []
        warnings: list[Reason] = []
        if agenda.date < date.today():
            issues.append(AgendaIssue(None, RC.AGENDA_PAST, "agenda is in the past"))
        if len(agenda.focus) > 3:
            issues.append(AgendaIssue(None, RC.FOCUS_INVALID, "focus has more than 3"))
        if len(set(agenda.focus)) != len(agenda.focus):
            issues.append(
                AgendaIssue(None, RC.FOCUS_INVALID, "focus has duplicates")
            )
        if not agenda.items:
            issues.append(AgendaIssue(None, RC.AGENDA_EMPTY, "agenda has no items"))

        work = [item for item in agenda.items if item.kind == "work"]
        chores = [item for item in agenda.items if item.kind == "manual"]
        if len(work) > agenda.capacity.work_items:
            issues.append(
                AgendaIssue(
                    None,
                    RC.OVER_CAPACITY,
                    f"{len(work)} work items exceeds {agenda.capacity.work_items}",
                )
            )
        if len(chores) > agenda.capacity.chores:
            warnings.append(
                Reason(
                    RC.OVER_CAPACITY,
                    f"{len(chores)} chores exceeds {agenda.capacity.chores}",
                )
            )

        seen: set[str] = set()
        for item in agenda.items:
            if item.task in seen:
                issues.append(
                    AgendaIssue(item.id, RC.DUPLICATE_TASK, f"{item.task} repeated")
                )
            seen.add(item.task)

        state = self.taskstore.state()
        by_id = {project.id: project for project in state.projects}
        statuses = task_status_map(state.projects)
        for project_id in agenda.focus:
            project = by_id.get(project_id)
            if project is None:
                issues.append(
                    AgendaIssue(None, RC.PROJECT_MISSING, f"focus {project_id}")
                )
            elif project.status in ("parked", "archived"):
                issues.append(
                    AgendaIssue(
                        None,
                        RC.PROJECT_PARKED
                        if project.status == "parked"
                        else RC.PROJECT_ARCHIVED,
                        f"focus {project_id} is {project.status}",
                    )
                )

        work_order = {item.task: item.order for item in work}
        work_items = [
            {"id": item.id, "project": item.project, "task": item.task}
            for item in work
        ]
        deps: dict[str, list[str]] = {}
        for item in work:
            project = by_id.get(item.project)
            if project is None:
                issues.append(
                    AgendaIssue(item.id, RC.PROJECT_MISSING, item.project)
                )
                continue
            context = self.eligibility.context(project)
            if context.plan is not None:
                deps[item.task] = context.plan.deps_for(item.task)
            objective = next(
                (obj for obj in project.objectives if obj.id == item.objective),
                None,
            )
            task = next(
                (
                    task
                    for obj in project.objectives
                    for task in obj.tasks
                    if task.id == item.task
                ),
                None,
            )
            if objective is None or task is None:
                issues.append(
                    AgendaIssue(
                        item.id,
                        RC.PROJECT_MISSING,
                        f"{item.objective}/{item.task} not found",
                    )
                )
                continue
            if item.status == "done":
                # A finished item is not a claim about the future: its task is
                # done in TaskStore, so eligibility has nothing left to prove.
                continue
            check = self.eligibility.check_task_for(
                project,
                objective,
                task,
                statuses,
                agenda_order=work_order,
            )
            for reason in check.reasons:
                issues.append(AgendaIssue(item.id, reason.code, reason.detail))
            warnings.extend(check.warnings)
        for task_id in _cycles(work_items, deps):
            item = next(
                (candidate for candidate in work if candidate.task == task_id), None
            )
            if item is not None:
                issues.append(
                    AgendaIssue(item.id, RC.CYCLE, "dependency cycle on the agenda")
                )

        for item in chores:
            project = by_id.get(item.project)
            if project is None:
                issues.append(
                    AgendaIssue(item.id, RC.PROJECT_MISSING, item.project)
                )
                continue
            if project.status in ("parked", "archived"):
                issues.append(
                    AgendaIssue(
                        item.id,
                        RC.PROJECT_PARKED
                        if project.status == "parked"
                        else RC.PROJECT_ARCHIVED,
                        f"{project.id} is {project.status}",
                    )
                )
                continue
            objective = next(
                (obj for obj in project.objectives if obj.id == item.objective),
                None,
            )
            task = next(
                (
                    task
                    for obj in project.objectives
                    for task in obj.tasks
                    if task.id == item.task
                ),
                None,
            )
            if objective is None or task is None:
                issues.append(
                    AgendaIssue(
                        item.id,
                        RC.PROJECT_MISSING,
                        f"{item.objective}/{item.task} not found",
                    )
                )
            elif task.status != "open" and item.status != "done":
                issues.append(
                    AgendaIssue(
                        item.id, RC.TASK_NOT_OPEN, f"{item.task} is {task.status}"
                    )
                )

        if issues:
            raise AgendaValidationError(issues)

        try:
            locked = self.taskstore.patch_agenda(
                day, {"status": "locked", "if_rev": agenda.rev}
            )
        except TaskStoreError as exc:
            if exc.status_code == 409:
                raise AgendaError("agenda changed; reload before locking") from exc
            raise
        self._store_snapshot(locked)
        db.record_event(
            self.conn,
            "agenda.locked",
            {"date": day.isoformat(), "rev": locked.rev},
        )
        return LockResult(locked, warnings=warnings)

    def unlock(self, day: date, reason: str) -> TaskStoreAgenda:
        if not reason.strip():
            raise AgendaError("unlock requires a reason")
        agenda = self.taskstore.get_agenda(day)
        if agenda is None:
            raise AgendaError(f"no agenda for {day.isoformat()}")
        if agenda.status != "locked":
            raise AgendaError("agenda is not locked")
        updated = self.taskstore.patch_agenda(
            day,
            {
                "status": "draft",
                "unlock_reason": reason.strip(),
                "if_rev": agenda.rev,
            },
        )
        db.delete_snapshot(self.conn, day.isoformat())
        db.record_event(
            self.conn,
            "agenda.unlocked",
            {"date": day.isoformat(), "rev": updated.rev, "reason": reason.strip()},
        )
        return updated

    def _store_snapshot(self, agenda: TaskStoreAgenda) -> None:
        locked_at = agenda.locked_at or datetime.now(UTC)
        payload = {
            "date": agenda.date.isoformat(),
            "rev": agenda.rev,
            "focus": agenda.focus,
            "capacity": agenda.capacity.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in agenda.items],
            "locked_at": locked_at.isoformat().replace("+00:00", "Z"),
        }
        db.put_snapshot(
            self.conn,
            agenda.date.isoformat(),
            agenda.rev,
            payload["locked_at"],
            payload,
        )
