from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from . import db
from .config import Config
from .eligibility import EligibilityService
from .errors import RunError
from .runner import RunEngine
from .taskstore import TaskStoreClient


@dataclass
class BuildStatus:
    project: str
    active: bool = False
    current_run: str | None = None
    merged: int = 0
    note: str | None = None
    started: str | None = None
    stop_requested: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "active": self.active,
            "current_run": self.current_run,
            "merged": self.merged,
            "note": self.note,
            "started": self.started,
        }


class ProjectBuilder:
    """Build one project's locked agenda, one eligible task at a time.

    A merge marks the task done in TaskStore, which unblocks its dependents; the
    loop re-reads eligibility after every run instead of precomputing an order.
    """

    def __init__(
        self, config: Config, taskstore: TaskStoreClient, engine: RunEngine
    ) -> None:
        self.config = config
        self.taskstore = taskstore
        self.engine = engine
        self._lock = threading.Lock()
        self._status: dict[str, BuildStatus] = {}

    # ------------------------------------------------------------------ api
    def status(self, project: str) -> dict[str, Any]:
        with self._lock:
            status = self._status.get(project)
            payload = (
                status.to_dict()
                if status is not None
                else {"project": project, "active": False, "current_run": None,
                      "merged": 0, "note": None, "started": None}
            )
        payload["planned"] = self._planned(project)
        return payload

    def start(
        self, project: str, model: str | None = None, reviewer: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            current = self._status.get(project)
            if current is not None and current.active:
                raise RunError(f"{project} is already building")
            agenda = self.taskstore.get_agenda(date.today())
            if agenda is None or agenda.status != "locked":
                raise RunError("lock today's agenda before building a project")
            if not any(
                item.project == project and item.status == "planned"
                for item in agenda.items
            ):
                raise RunError(f"no planned agenda items for {project}")
            status = BuildStatus(
                project=project,
                active=True,
                started=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            status.thread = threading.Thread(
                target=self._run, args=(project, status, model, reviewer),
                daemon=True, name=f"cc-build-{project}",
            )
            self._status[project] = status
        status.thread.start()
        return self.status(project)

    def stop(self, project: str) -> dict[str, Any]:
        with self._lock:
            status = self._status.get(project)
            if status is None or not status.active:
                raise RunError(f"{project} is not building")
            status.stop_requested = True
            run_id = status.current_run
        if run_id:
            conn = db.connect(self.config.state_dir)
            try:
                db.request_cancel(conn, run_id)
            finally:
                conn.close()
        return self.status(project)

    # --------------------------------------------------------------- worker
    def _run(
        self,
        project: str,
        status: BuildStatus,
        model: str | None,
        reviewer: str | None,
    ) -> None:
        try:
            while True:
                if status.stop_requested:
                    status.note = "stopped"
                    return
                agenda = self.taskstore.get_agenda(date.today())
                if agenda is None or agenda.status != "locked":
                    status.note = "agenda is no longer locked"
                    return
                items = [
                    item
                    for item in agenda.items
                    if item.project == project and item.status == "planned"
                ]
                if not items:
                    status.note = "nothing left to build"
                    return
                candidate, blocked = self._next_ready(project, items)
                if candidate is None:
                    status.note = blocked or "nothing is eligible yet"
                    return
                try:
                    record = self.engine.create(candidate.task, model, reviewer)
                except RunError as exc:
                    status.note = str(exc)
                    return
                status.current_run = record.id
                finished = self.engine.execute(record)
                if finished.state != "merged":
                    reason = finished.error or finished.state
                    status.note = f"{finished.id} {reason}"
                    return
                status.merged += 1
        except Exception as exc:  # noqa: BLE001 - the worker must not die silently
            status.note = f"build stopped: {exc}"
        finally:
            with self._lock:
                status.active = False
                status.current_run = None

    def _next_ready(self, project: str, items: list[Any]):
        conn = db.connect(self.config.state_dir)
        try:
            checks = EligibilityService(
                conn, self.taskstore, self.config
            ).check_all()
        finally:
            conn.close()
        ready = {
            check.task.id
            for check in checks
            if check.project.id == project and check.result.ready
        }
        for item in items:
            if item.task in ready:
                return item, None
        blocked = None
        for check in checks:
            if check.project.id != project:
                continue
            reason = next(
                (row for row in check.result.reasons if row.code == "dep_missing"),
                None,
            )
            if reason is not None:
                blocked = f"nothing ready — {reason.detail}"
                break
        return None, blocked

    def _planned(self, project: str) -> int:
        try:
            agenda = self.taskstore.get_agenda(date.today())
        except Exception:  # noqa: BLE001 - status must never fail
            return 0
        if agenda is None:
            return 0
        return sum(
            1
            for item in agenda.items
            if item.project == project and item.status == "planned"
        )
