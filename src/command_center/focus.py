from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import db
from .errors import FocusError, TaskStoreError
from .taskstore import TaskStoreClient, TaskStoreProject

STALE_AFTER = timedelta(days=7)
INACTIVE = {"parked", "archived"}


@dataclass(frozen=True)
class FocusEntry:
    project_id: str
    position: int
    set_at: str


@dataclass
class FocusState:
    entries: list[FocusEntry]
    projects: list[TaskStoreProject] = field(default_factory=list)
    stale: bool = False
    tag_errors: list[str] = field(default_factory=list)


class FocusService:
    def __init__(self, conn: sqlite3.Connection, taskstore: TaskStoreClient):
        self.conn = conn
        self.taskstore = taskstore

    def list(self) -> FocusState:
        state = self.taskstore.state()
        return self._sync(state.projects)

    def set(self, project_ids: list[str]) -> FocusState:
        if len(set(project_ids)) != len(project_ids):
            raise FocusError("focus contains duplicate project ids")
        if len(project_ids) > 3:
            raise FocusError("focus allows at most three projects")
        state = self.taskstore.state()
        by_id = {project.id: project for project in state.projects}
        for project_id in project_ids:
            project = by_id.get(project_id)
            if project is None:
                raise FocusError(f"project {project_id!r} not found")
            if project.status in INACTIVE:
                raise FocusError(f"project {project_id!r} is {project.status}")
        db.set_focus(self.conn, project_ids)
        db.record_event(
            self.conn,
            "focus.set",
            {"projects": project_ids},
        )
        return self._sync(state.projects)

    def _sync(self, projects: list[TaskStoreProject]) -> FocusState:
        by_id = {project.id: project for project in projects}
        rows = db.focus_rows(self.conn)

        if not rows:
            imported = sorted(
                (
                    project
                    for project in projects
                    if project.focus_order is not None
                    and project.status not in INACTIVE
                ),
                key=lambda project: (project.focus_order or 99, project.name.lower()),
            )[:3]
            if imported:
                db.set_focus(
                    self.conn,
                    [project.id for project in imported],
                    db.now_iso(),
                )
                rows = db.focus_rows(self.conn)

        valid = [
            row
            for row in rows
            if row["project_id"] in by_id
            and by_id[row["project_id"]].status not in INACTIVE
        ]
        if len(valid) != len(rows):
            db.set_focus(
                self.conn,
                [row["project_id"] for row in valid],
                rows[0]["set_at"] if rows else db.now_iso(),
            )
            rows = db.focus_rows(self.conn)

        desired = {row["project_id"]: row["position"] for row in rows}
        errors: list[str] = []
        for project in projects:
            want = desired.get(project.id)
            if project.focus_order == want:
                continue
            try:
                self._write_tag(project, want)
            except TaskStoreError as exc:
                errors.append(f"{project.id}: {exc}")

        entries = [
            FocusEntry(
                project_id=row["project_id"],
                position=row["position"],
                set_at=row["set_at"],
            )
            for row in db.focus_rows(self.conn)
        ]
        stale = False
        if entries:
            try:
                oldest = min(
                    datetime.fromisoformat(entry.set_at.replace("Z", "+00:00"))
                    for entry in entries
                )
                stale = datetime.now(UTC) - oldest > STALE_AFTER
            except ValueError:
                stale = False
        focused_projects = [
            by_id[entry.project_id]
            for entry in entries
            if entry.project_id in by_id
        ]
        return FocusState(entries, focused_projects, stale, errors)

    def _write_tag(self, project: TaskStoreProject, position: int | None) -> None:
        payload = {"focus_order": position, "if_rev": project.rev}
        try:
            self.taskstore.patch_project(project.id, payload)
        except TaskStoreError as exc:
            if exc.status_code != 409:
                raise
            latest = self.taskstore.get_project(project.id)
            payload["if_rev"] = latest.rev
            self.taskstore.patch_project(project.id, payload)
