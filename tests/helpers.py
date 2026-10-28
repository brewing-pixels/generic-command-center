from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from command_center.agent import AgentResult
from command_center.errors import TaskStoreError
from command_center.probe import ModelStatus
from command_center.taskstore import (
    TaskStoreAgenda,
    TaskStoreAgendaItem,
    TaskStoreObjective,
    TaskStoreProject,
    TaskStoreSpecState,
    TaskStoreState,
    TaskStoreTask,
)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def commit(repo: Path, message: str = "update") -> None:
    if not git(repo, "status", "--porcelain").stdout.strip():
        return
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def spec_state(docs: list[str], digest: str, phase: str = "approved") -> TaskStoreSpecState:
    return TaskStoreSpecState(
        docs=docs,
        digest=digest,
        phase=phase,
        approved_at=datetime.now(UTC),
        approved_in_commit="0" * 40,
        dirty=False,
    )


def make_project(
    *,
    project_id: str = "p1",
    name: str = "Project",
    path: str = "",
    status: str = "ongoing",
    rev: int = 0,
    focus_order: int | None = None,
    spec_state_value: TaskStoreSpecState | None = None,
    tasks: list[TaskStoreTask] | None = None,
    objectives: list[TaskStoreObjective] | None = None,
) -> TaskStoreProject:
    if objectives is None:
        objectives = [
            TaskStoreObjective(
                id="o_general",
                title="General",
                tasks=tasks or [TaskStoreTask(id="t1", title="Task one")],
            )
        ]
    return TaskStoreProject(
        id=project_id,
        name=name,
        status=status,
        path=path,
        rev=rev,
        focus_order=focus_order,
        spec_state=spec_state_value,
        objectives=objectives,
    )


def make_agenda(
    *,
    day: date | None = None,
    items: list[TaskStoreAgendaItem] | None = None,
    focus: list[str] | None = None,
    work_items: int = 3,
    chores: int = 3,
    rev: int = 0,
) -> TaskStoreAgenda:
    return TaskStoreAgenda(
        date=day or date.today(),
        rev=rev,
        focus=focus or [],
        capacity={
            "work_items": work_items,
            "chores": chores,
            "operator_min": 90,
        },
        items=items or [],
    )


def work_item(
    *,
    item_id: str = "a_01",
    project: str = "p1",
    task: str = "t1",
    order: int = 0,
) -> TaskStoreAgendaItem:
    return TaskStoreAgendaItem(
        id=item_id,
        kind="work",
        project=project,
        objective="o_general",
        task=task,
        order=order,
    )


def manual_item(
    *,
    item_id: str = "a_01",
    project: str = "p1",
    task: str = "t1",
    order: int = 0,
) -> TaskStoreAgendaItem:
    return TaskStoreAgendaItem(
        id=item_id,
        kind="manual",
        project=project,
        objective="o_general",
        task=task,
        order=order,
    )


class FakeProbe:
    def __init__(self, up: bool = True):
        self.up = up

    def role_status(self, role: str) -> ModelStatus:
        return ModelStatus(
            "hosted",
            "Hosted model",
            "provider/model-name",
            self.up,
            False,
            "API key present" if self.up else "PROVIDER_API_KEY is not set",
        )


class FakeAgent:
    def __init__(
        self,
        path: str | None = "src/x.py",
        content: str = "hello\n",
        review_verdict: str = "approve",
    ):
        self.path = path
        self.content = content
        self.review_verdict = review_verdict
        self.calls: list[str] = []
        self.sessions: list[str | None] = []
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        run_dir: Path,
        model: str,
        agent: str = "cc-coder",
        append: bool = False,
        session_id: str | None = None,
    ) -> AgentResult:
        self.calls.append(agent)
        self.sessions.append(session_id)
        self.prompts.append(prompt)
        if agent == "cc-reviewer":
            return AgentResult(
                session_id or "ses_review",
                json.dumps(
                    {
                        "verdict": self.review_verdict,
                        "reasons": ["fake review"],
                        "findings": [],
                    }
                ),
                0,
            )
        if self.path:
            target = cwd / self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.content, encoding="utf-8")
        return AgentResult(session_id or "ses_test", "done", 0)


class FakeTaskStore:
    def __init__(
        self,
        projects: list[TaskStoreProject] | None = None,
        agenda: TaskStoreAgenda | None = None,
    ):
        self.projects = {project.id: project for project in projects or []}
        self.agenda = agenda
        self.project_calls: list[tuple[str, dict]] = []
        self.agenda_calls: list[dict] = []
        self.task_calls: list[tuple[str, dict]] = []
        self.agenda_item_calls: list[tuple[str, dict]] = []

    def state(self) -> TaskStoreState:
        return TaskStoreState(projects=list(self.projects.values()))

    def get_project(self, project_id: str) -> TaskStoreProject:
        if project_id not in self.projects:
            raise TaskStoreError(f"project {project_id} not found", 404)
        return self.projects[project_id]

    def patch_project(self, project_id: str, payload: dict) -> TaskStoreProject:
        if project_id not in self.projects:
            raise TaskStoreError(f"project {project_id} not found", 404)
        project = self.projects[project_id]
        if "if_rev" in payload and payload["if_rev"] != project.rev:
            raise TaskStoreError("stale", 409, {"current_rev": project.rev})
        data = project.model_dump(mode="json")
        data.update({key: value for key, value in payload.items() if key != "if_rev"})
        data["rev"] = project.rev + 1
        updated = TaskStoreProject.model_validate(data)
        self.projects[project_id] = updated
        self.project_calls.append((project_id, payload))
        return updated

    def create_project(self, payload: dict) -> TaskStoreProject:
        project_id = payload.get("id") or f"p_quick_{len(self.projects) + 1:04d}"
        project = make_project(
            project_id=project_id,
            name=payload["name"],
            path=payload.get("path", ""),
            objectives=[
                TaskStoreObjective(
                    id="o_general",
                    title="General",
                    tasks=[],
                )
            ],
        )
        self.projects[project_id] = project
        self.project_calls.append((project_id, payload))
        return project

    def create_objective(self, project_id: str, payload: dict) -> TaskStoreObjective:
        project = self.projects.get(project_id)
        if project is None:
            raise TaskStoreError("project not found", 404)
        data = project.model_dump(mode="json")
        existing = {objective["id"] for objective in data["objectives"]}
        index = 1
        while f"o_quick_{index:04d}" in existing:
            index += 1
        objective = TaskStoreObjective(
            id=f"o_quick_{index:04d}",
            title=payload["title"],
            tasks=[],
        )
        data["objectives"].append(objective.model_dump(mode="json"))
        data["rev"] = project.rev + 1
        self.projects[project_id] = TaskStoreProject.model_validate(data)
        return objective

    def create_task(
        self,
        project_id: str,
        objective_id: str,
        payload: dict,
    ) -> TaskStoreTask:
        project = self.projects.get(project_id)
        if project is None:
            raise TaskStoreError("project not found", 404)
        data = project.model_dump(mode="json")
        objective = next(
            (item for item in data["objectives"] if item["id"] == objective_id),
            None,
        )
        if objective is None:
            raise TaskStoreError("objective not found", 404)
        existing = {task["id"] for task in objective["tasks"]}
        index = 1
        while f"t_quick_{index:04d}" in existing:
            index += 1
        task = TaskStoreTask(
            id=f"t_quick_{index:04d}",
            title=payload["title"],
            note=payload.get("note"),
            order=len(objective["tasks"]) * 10,
        )
        objective["tasks"].append(task.model_dump(mode="json"))
        data["rev"] = project.rev + 1
        self.projects[project_id] = TaskStoreProject.model_validate(data)
        return task

    def patch_task(
        self,
        project_id: str,
        objective_id: str,
        task_id: str,
        payload: dict,
    ) -> TaskStoreTask:
        project = self.projects[project_id]
        data = project.model_dump(mode="json")
        objective = next(
            (item for item in data["objectives"] if item["id"] == objective_id),
            None,
        )
        if objective is None:
            raise TaskStoreError("objective not found", 404)
        task = next(
            (item for item in objective["tasks"] if item["id"] == task_id),
            None,
        )
        if task is None:
            raise TaskStoreError("task not found", 404)
        if "if_rev" in payload and payload["if_rev"] != task["rev"]:
            raise TaskStoreError("stale", 409, {"current_rev": task["rev"]})
        for key, value in payload.items():
            if key != "if_rev":
                task[key] = value
        task["rev"] = task["rev"] + 1
        data["rev"] = project.rev + 1
        updated = TaskStoreProject.model_validate(data)
        self.projects[project_id] = updated
        self.task_calls.append((task_id, payload))
        return next(
            item
            for objective in updated.objectives
            for item in objective.tasks
            if item.id == task_id
        )

    def get_agenda(self, day: date) -> TaskStoreAgenda | None:
        if self.agenda is None or self.agenda.date != day:
            return None
        return self.agenda

    def patch_agenda(self, day: date, payload: dict) -> TaskStoreAgenda:
        if self.agenda is None or self.agenda.date != day:
            raise TaskStoreError("agenda not found", 404)
        if "if_rev" in payload and payload["if_rev"] != self.agenda.rev:
            raise TaskStoreError("stale", 409, {"current_rev": self.agenda.rev})
        data = self.agenda.model_dump(mode="json")
        if "status" in payload:
            data["status"] = payload["status"]
            data["rev"] = self.agenda.rev + 1
            if payload["status"] == "locked":
                data["locked_at"] = datetime.now(UTC).isoformat()
            else:
                data["locked_at"] = None
                data["unlock_reason"] = payload.get("unlock_reason")
        updated = TaskStoreAgenda.model_validate(data)
        self.agenda = updated
        self.agenda_calls.append(payload)
        return updated

    def add_agenda_item(self, day: date, payload: dict) -> TaskStoreAgenda:
        if self.agenda is None or self.agenda.date != day:
            raise TaskStoreError("agenda not found", 404)
        if self.agenda.status == "locked":
            raise TaskStoreError("agenda is locked", 409)
        if "if_rev" in payload and payload["if_rev"] != self.agenda.rev:
            raise TaskStoreError("stale", 409, {"current_rev": self.agenda.rev})
        data = self.agenda.model_dump(mode="json")
        existing = {item["id"] for item in data["items"]}
        index = 1
        while f"a_{index:02d}" in existing:
            index += 1
        item = TaskStoreAgendaItem(
            id=f"a_{index:02d}",
            kind=payload["kind"],
            project=payload["project"],
            objective=payload["objective"],
            task=payload["task"],
            reason=payload.get("reason", ""),
            estimate_min=payload.get("estimate_min"),
            due_time=payload.get("due_time"),
            order=len(data["items"]),
            note=payload.get("note"),
        )
        data["items"].append(item.model_dump(mode="json"))
        data["rev"] = self.agenda.rev + 1
        self.agenda = TaskStoreAgenda.model_validate(data)
        return self.agenda

    def patch_agenda_item(
        self, day: date, item_id: str, payload: dict
    ) -> TaskStoreAgenda:
        if self.agenda is None or self.agenda.date != day:
            raise TaskStoreError("agenda not found", 404)
        if "if_rev" in payload and payload["if_rev"] != self.agenda.rev:
            raise TaskStoreError("stale", 409, {"current_rev": self.agenda.rev})
        data = self.agenda.model_dump(mode="json")
        item = next(
            (row for row in data["items"] if row["id"] == item_id), None
        )
        if item is None:
            raise TaskStoreError("agenda item not found", 404)
        for key, value in payload.items():
            if key != "if_rev":
                item[key] = value
        updated = TaskStoreAgenda.model_validate(data)
        self.agenda = updated
        self.agenda_item_calls.append((item_id, payload))
        return updated


    def put_agenda(self, day: date, payload: dict) -> TaskStoreAgenda:
        if self.agenda is not None and self.agenda.date == day:
            if "if_rev" in payload and payload["if_rev"] != self.agenda.rev:
                raise TaskStoreError("stale", 409, {"current_rev": self.agenda.rev})
            rev = self.agenda.rev + 1
            status = self.agenda.status
        else:
            rev = 0
            status = "draft"
        data = dict(payload)
        data["date"] = day.isoformat()
        data.setdefault("status", status)
        data["rev"] = rev
        items = []
        for index, item in enumerate(data.get("items", [])):
            row = dict(item)
            row.setdefault("id", f"a_{index:02d}")
            row.setdefault("status", "planned")
            row.setdefault("order", index)
            items.append(row)
        data["items"] = items
        self.agenda = TaskStoreAgenda.model_validate(data)
        return self.agenda

    def suggest_agenda(
        self, day: date, *, replace: bool = False, **kwargs
    ) -> TaskStoreAgenda:
        if self.agenda is not None and not replace:
            raise TaskStoreError("agenda exists", 409)
        assert self.agenda is not None
        return self.agenda


class FakeInquisitor:
    def __init__(self, drafts: list[dict[str, str]]):
        self.drafts = drafts
        self.calls = 0
        self.sessions: list[str | None] = []
        self.agents: list[str] = []
        self.models: list[str] = []
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        run_dir: Path,
        model: str,
        agent: str = "cc-coder",
        append: bool = False,
        session_id: str | None = None,
    ) -> AgentResult:
        self.calls += 1
        self.prompts.append(prompt)
        self.sessions.append(session_id)
        self.agents.append(agent)
        self.models.append(model)
        files = self.drafts[min(self.calls - 1, len(self.drafts) - 1)]
        for name, text in files.items():
            path = cwd / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return AgentResult(session_id or "ses_arch", "done", 0)
