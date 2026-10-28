from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db, gitutil, spec
from .config import Config
from .errors import PlanError
from .paths import project_root
from .plan import Plan, load_plan_ref
from .probe import ModelProbe
from .taskstore import TaskStoreClient, TaskStoreObjective, TaskStoreProject, TaskStoreTask


class RC:
    PROJECT_MISSING = "project_missing"
    NOT_GIT = "not_git"
    PROJECT_PARKED = "project_parked"
    PROJECT_ARCHIVED = "project_archived"
    OBJECTIVE_CLOSED = "objective_closed"
    TASK_NOT_OPEN = "task_not_open"
    NO_GATE = "no_gate"
    DEP_MISSING = "dep_missing"
    DEP_LATER = "dep_later"
    CYCLE = "cycle"
    DUPLICATE_TASK = "duplicate_task"
    SPEC_UNAPPROVED = "spec_unapproved"
    SPEC_DIRTY = "spec_dirty"
    BASE_DIRTY = "base_dirty"
    TASK_LOCKED = "task_locked"
    AGENDA_NOT_LOCKED = "agenda_not_locked"
    AGENDA_STALE = "agenda_stale"
    NOT_ON_AGENDA = "not_on_agenda"
    PROJECT_BUSY = "project_busy"
    MODEL_DOWN = "model_down"
    OVER_CAPACITY = "over_capacity"
    AGENDA_LOCKED = "agenda_locked"
    AGENDA_EMPTY = "agenda_empty"
    AGENDA_PAST = "agenda_past"
    FOCUS_INVALID = "focus_invalid"
    STALE_REV = "stale_rev"
    SPEC_CHECK_FAILED = "spec_check_failed"
    GATE_DIRTY = "gate_dirty"


INACTIVE = {"parked", "archived"}


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str


@dataclass
class CheckResult:
    ready: bool
    reasons: list[Reason] = field(default_factory=list)
    warnings: list[Reason] = field(default_factory=list)


@dataclass
class TaskCheck:
    project: TaskStoreProject
    objective: TaskStoreObjective
    task: TaskStoreTask
    result: CheckResult


@dataclass
class ProjectContext:
    project: TaskStoreProject
    root: Path | None
    plan: Plan | None
    plan_error: str | None
    is_git: bool
    clean: bool
    spec_check: spec.SpecCheck


def task_status_map(projects: list[TaskStoreProject]) -> dict[str, str]:
    return {
        task.id: task.status
        for project in projects
        for objective in project.objectives
        for task in objective.tasks
    }


class EligibilityService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        taskstore: TaskStoreClient,
        config: Config,
        probe: ModelProbe | None = None,
    ):
        self.conn = conn
        self.taskstore = taskstore
        self.config = config
        self.probe = probe or ModelProbe(config)
        self._contexts: dict[str, ProjectContext] = {}
        self._model_warnings: list[Reason] | None = None

    def check_all(self) -> list[TaskCheck]:
        state = self.taskstore.state()
        statuses = task_status_map(state.projects)
        results: list[TaskCheck] = []
        for project in state.projects:
            context = self._context(project)
            for objective, task in project.tasks:
                results.append(
                    TaskCheck(
                        project,
                        objective,
                        task,
                        self._check(context, objective, task, statuses),
                    )
                )
        return results

    def check_task_for(
        self,
        project: TaskStoreProject,
        objective: TaskStoreObjective,
        task: TaskStoreTask,
        statuses: dict[str, str],
        agenda_order: dict[str, int] | None = None,
    ) -> CheckResult:
        context = self._contexts.get(project.id)
        if context is None:
            context = self._context(project)
        return self._check(context, objective, task, statuses, agenda_order)

    def context(self, project: TaskStoreProject) -> ProjectContext:
        context = self._contexts.get(project.id)
        return context if context is not None else self._context(project)

    def _context(self, project: TaskStoreProject) -> ProjectContext:
        cached = self._contexts.get(project.id)
        if cached is not None:
            return cached
        root = project_root(project.path)
        plan: Plan | None = None
        plan_error: str | None = None
        is_git = False
        clean = False
        spec_check = spec.SpecCheck(False, RC.SPEC_UNAPPROVED, "project has no path")
        if root is not None and root.exists():
            try:
                is_git = gitutil.is_git_repo(root)
            except gitutil.GitError as exc:
                plan_error = str(exc)
            if is_git:
                try:
                    plan = load_plan_ref(root, gitutil.base_ref(root))
                except PlanError as exc:
                    plan_error = str(exc)
                try:
                    clean = gitutil.is_clean(root)
                except gitutil.GitError:
                    clean = False
                spec_check = spec.check(project)
        context = ProjectContext(
            project=project,
            root=root,
            plan=plan,
            plan_error=plan_error,
            is_git=is_git,
            clean=clean,
            spec_check=spec_check,
        )
        self._contexts[project.id] = context
        return context

    def _check(
        self,
        context: ProjectContext,
        objective: TaskStoreObjective,
        task: TaskStoreTask,
        statuses: dict[str, str],
        agenda_order: dict[str, int] | None = None,
    ) -> CheckResult:
        project = context.project
        reasons: list[Reason] = []
        if project.status == "parked":
            reasons.append(Reason(RC.PROJECT_PARKED, "project is parked"))
        elif project.status == "archived":
            reasons.append(Reason(RC.PROJECT_ARCHIVED, "project is archived"))
        if context.root is None or not context.root.exists():
            reasons.append(Reason(RC.PROJECT_MISSING, project.path or "no path"))
            return CheckResult(False, reasons, self._warnings())
        if not context.is_git:
            reasons.append(Reason(RC.NOT_GIT, context.plan_error or "not a git repo"))
            return CheckResult(False, reasons, self._warnings())
        if objective.status not in ("open", "doing"):
            reasons.append(
                Reason(RC.OBJECTIVE_CLOSED, f"objective is {objective.status}")
            )
        if task.status != "open":
            reasons.append(Reason(RC.TASK_NOT_OPEN, f"task is {task.status}"))
        if context.plan is None:
            reasons.append(
                Reason(RC.NO_GATE, context.plan_error or "no command-center.yaml")
            )
            return CheckResult(False, reasons, self._warnings())
        gate = context.plan.gate_for(objective.id, task.id)
        if gate is None:
            reasons.append(Reason(RC.NO_GATE, "task resolves to an empty gate"))
            return CheckResult(False, reasons, self._warnings())

        for dep in context.plan.deps_for(task.id):
            if statuses.get(dep) == "done":
                continue
            if agenda_order is not None and dep in agenda_order:
                if agenda_order[dep] < agenda_order[task.id]:
                    continue
                reasons.append(
                    Reason(RC.DEP_LATER, f"{dep} is scheduled after this task")
                )
                continue
            reasons.append(Reason(RC.DEP_MISSING, f"{dep} is not done"))
        if not context.spec_check.ok:
            reasons.append(
                Reason(
                    context.spec_check.code or RC.SPEC_UNAPPROVED,
                    context.spec_check.detail or "spec is not approved",
                )
            )
        if not context.clean:
            reasons.append(Reason(RC.BASE_DIRTY, "base checkout is dirty"))
        if db.lock_exists(self.conn, task.id):
            reasons.append(Reason(RC.TASK_LOCKED, "a live lock exists"))
        return CheckResult(not reasons, reasons, self._warnings())

    def _warnings(self) -> list[Reason]:
        if self._model_warnings is None:
            status = self.probe.role_status("coder")
            if status is not None and not status.up:
                self._model_warnings = [
                    Reason(RC.MODEL_DOWN, f"{status.label}: {status.reason}")
                ]
            else:
                self._model_warnings = []
        return list(self._model_warnings)
