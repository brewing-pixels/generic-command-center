from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .errors import TaskStoreError, TaskStoreNotFound


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TaskStoreSpecState(_Model):
    docs: list[str] = Field(default_factory=list)
    digest: str = ""
    approved_at: datetime | None = None
    approved_in_commit: str | None = None
    dirty: bool = False
    phase: str = "shape"


class TaskStoreLastRun(_Model):
    id: str
    verdict: str
    at: datetime


class TaskStoreTask(_Model):
    id: str
    title: str
    status: str = "open"
    priority: int = 0
    due: date | None = None
    note: str | None = None
    rev: int = 0
    order: int = 0
    last_run: TaskStoreLastRun | None = None


class TaskStoreObjective(_Model):
    id: str
    title: str
    status: str = "open"
    priority: int = 0
    rev: int = 0
    tasks: list[TaskStoreTask] = Field(default_factory=list)


class TaskStoreProject(_Model):
    id: str
    name: str
    status: str = "todo"
    priority: str = "N4"
    path: str = ""
    rev: int = 0
    focus_order: int | None = None
    spec_state: TaskStoreSpecState | None = None
    next: str = ""
    gate: str = ""
    summary: str = ""
    updated: datetime | None = None
    objectives: list[TaskStoreObjective] = Field(default_factory=list)

    @property
    def tasks(self) -> list[tuple[TaskStoreObjective, TaskStoreTask]]:
        return [
            (objective, task)
            for objective in self.objectives
            for task in objective.tasks
        ]


class TaskStoreState(_Model):
    projects: list[TaskStoreProject] = Field(default_factory=list)
    kpis: dict[str, Any] = Field(default_factory=dict)
    thesis: dict[str, Any] = Field(default_factory=dict)


class TaskStoreCapacity(_Model):
    work_items: int = 3
    chores: int = 3
    operator_min: int = 90


class TaskStoreAgendaItem(_Model):
    id: str
    kind: str
    project: str
    objective: str
    task: str
    reason: str = ""
    estimate_min: int | None = None
    due_time: str | None = None
    order: int = 0
    status: str = "planned"
    run: str | None = None
    note: str | None = None


class TaskStoreAgenda(_Model):
    date: date
    status: str = "draft"
    rev: int = 0
    focus: list[str] = Field(default_factory=list)
    capacity: TaskStoreCapacity = Field(default_factory=TaskStoreCapacity)
    items: list[TaskStoreAgendaItem] = Field(default_factory=list)
    locked_at: datetime | None = None
    unlock_reason: str | None = None
    generated_by: str | None = None


class TaskStoreClient:
    """Small synchronous client for the task store."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TaskStoreClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TaskStoreError(f"gateway unreachable: {exc}") from exc
        if response.status_code == 404:
            raise TaskStoreNotFound(f"{method} {path}: not found", 404)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"detail": response.text}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise TaskStoreError(
                f"{method} {path}: {detail or response.status_code}",
                response.status_code,
                payload if isinstance(payload, dict) else None,
            )
        if not response.content:
            return None
        return response.json()

    def state(self) -> TaskStoreState:
        return TaskStoreState.model_validate(self._request("GET", "/api/state"))

    def get_project(self, project_id: str) -> TaskStoreProject:
        return TaskStoreProject.model_validate(
            self._request("GET", f"/api/projects/{project_id}")
        )

    def patch_project(self, project_id: str, payload: dict[str, Any]) -> TaskStoreProject:
        return TaskStoreProject.model_validate(
            self._request("PATCH", f"/api/projects/{project_id}", json=payload)
        )

    def create_project(self, payload: dict[str, Any]) -> TaskStoreProject:
        return TaskStoreProject.model_validate(
            self._request("POST", "/api/projects", json=payload)
        )

    def create_objective(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> TaskStoreObjective:
        return TaskStoreObjective.model_validate(
            self._request(
                "POST",
                f"/api/projects/{project_id}/objectives",
                json=payload,
            )
        )

    def create_task(
        self,
        project_id: str,
        objective_id: str,
        payload: dict[str, Any],
    ) -> TaskStoreTask:
        return TaskStoreTask.model_validate(
            self._request(
                "POST",
                f"/api/projects/{project_id}/objectives/{objective_id}/tasks",
                json=payload,
            )
        )

    def patch_task(
        self,
        project_id: str,
        objective_id: str,
        task_id: str,
        payload: dict[str, Any],
    ) -> TaskStoreTask:
        return TaskStoreTask.model_validate(
            self._request(
                "PATCH",
                f"/api/projects/{project_id}/objectives/{objective_id}"
                f"/tasks/{task_id}",
                json=payload,
            )
        )

    def get_agenda(self, day: date) -> TaskStoreAgenda | None:
        try:
            data = self._request("GET", f"/api/agendas/{day.isoformat()}")
        except TaskStoreNotFound:
            return None
        return TaskStoreAgenda.model_validate(data)

    def put_agenda(self, day: date, payload: dict[str, Any]) -> TaskStoreAgenda:
        return TaskStoreAgenda.model_validate(
            self._request("PUT", f"/api/agendas/{day.isoformat()}", json=payload)
        )

    def suggest_agenda(
        self,
        day: date,
        *,
        replace: bool = False,
        model: str | None = None,
        if_rev: int | None = None,
    ) -> TaskStoreAgenda:
        payload: dict[str, Any] = {"replace": replace}
        if model is not None:
            payload["model"] = model
        if if_rev is not None:
            payload["if_rev"] = if_rev
        return TaskStoreAgenda.model_validate(
            self._request(
                "POST", f"/api/agendas/{day.isoformat()}/suggest", json=payload
            )
        )

    def add_agenda_item(self, day: date, payload: dict[str, Any]) -> TaskStoreAgenda:
        return TaskStoreAgenda.model_validate(
            self._request(
                "POST", f"/api/agendas/{day.isoformat()}/items", json=payload
            )
        )

    def patch_agenda(self, day: date, payload: dict[str, Any]) -> TaskStoreAgenda:
        return TaskStoreAgenda.model_validate(
            self._request("PATCH", f"/api/agendas/{day.isoformat()}", json=payload)
        )

    def patch_agenda_item(
        self, day: date, item_id: str, payload: dict[str, Any]
    ) -> TaskStoreAgenda:
        return TaskStoreAgenda.model_validate(
            self._request(
                "PATCH",
                f"/api/agendas/{day.isoformat()}/items/{item_id}",
                json=payload,
            )
        )

    def delete_agenda_item(
        self, day: date, item_id: str, if_rev: int | None = None
    ) -> TaskStoreAgenda:
        params = {"if_rev": if_rev} if if_rev is not None else None
        return TaskStoreAgenda.model_validate(
            self._request(
                "DELETE",
                f"/api/agendas/{day.isoformat()}/items/{item_id}",
                params=params,
            )
        )
