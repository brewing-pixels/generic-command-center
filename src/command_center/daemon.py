from __future__ import annotations

import asyncio
import json
import queue
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, gitutil, overview
from .agenda import AgendaService, AgendaValidationError
from .build_queue import ProjectBuilder
from .config import REPO_ROOT, Config, load_config, save_roles
from .eligibility import EligibilityService, Reason, TaskCheck
from .errors import (
    AgendaError,
    CommandCenterError,
    FocusError,
    RunError,
    ShapeError,
    TaskStoreError,
)
from .events import EventBus
from .focus import FocusService
from .jobs import KINDS as JOB_KINDS
from .jobs import JobRunner
from .paths import project_root
from .probe import ModelProbe
from .runner import RunEngine
from .shape import (
    C4_DOC,
    USE_CASES_DOC,
    ShapeEngine,
    parse_c4,
    parse_use_cases,
)
from .spec import default_docs
from .taskstore import TaskStoreClient
from .token import ensure_token


class FocusRequest(BaseModel):
    projects: list[str]


class LockRequest(BaseModel):
    if_rev: int | None = None


class UnlockRequest(BaseModel):
    reason: str


class RunRequest(BaseModel):
    task: str
    model: str | None = None
    reviewer: str | None = None


class ShapeStartRequest(BaseModel):
    project: str
    rewrite: bool = False
    objective: str | None = None
    task: str | None = None
    model: str | None = None


class ShapeModelRequest(BaseModel):
    model: str


class RolesPatch(BaseModel):
    inquisitor: str | None = None
    coder: str | None = None
    reviewer: str | None = None
    chat: str | None = None


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = ""


class ObjectiveCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ProjectBuildRequest(BaseModel):
    model: str | None = None
    reviewer: str | None = None


class AgendaTaskRef(BaseModel):
    project: str
    objective: str
    task: str


class AgendaAddRequest(BaseModel):
    tasks: list[AgendaTaskRef] = Field(min_length=1)
    if_rev: int | None = None


class TaskCreateRequest(BaseModel):
    project: str
    objective: str
    title: str = Field(min_length=1, max_length=300)
    add_to_today: bool = False
    day: date | None = None
    if_rev: int | None = None


class ShapeJobRequest(BaseModel):
    kind: str
    message: str | None = None
    question_id: str | None = None
    answers: dict[str, str] | None = None
    scope: str | None = None


class PinRequest(BaseModel):
    pin: str


class QuickAddRequest(BaseModel):
    project: str
    title: str = Field(min_length=1, max_length=300)
    due_time: str | None = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    reason: str = ""
    if_rev: int | None = Field(default=None, ge=0)


def _reason(reason: Reason) -> dict[str, str]:
    return {"code": reason.code, "detail": reason.detail}


def _task_check(check: TaskCheck) -> dict[str, Any]:
    return {
        "project": check.project.id,
        "project_name": check.project.name,
        "objective": check.objective.id,
        "objective_title": check.objective.title,
        "task": check.task.id,
        "title": check.task.title,
        "status": check.task.status,
        "ready": check.result.ready,
        "reasons": [_reason(reason) for reason in check.result.reasons],
        "warnings": [_reason(reason) for reason in check.result.warnings],
    }


def create_app(
    config: Config,
    *,
    taskstore: TaskStoreClient | None = None,
    engine: RunEngine | None = None,
    bus: EventBus | None = None,
    shape_engine: ShapeEngine | None = None,
) -> FastAPI:
    token = ensure_token(config.token_path)
    bus = bus or EventBus()
    taskstore = taskstore or TaskStoreClient(config.taskstore.url)
    engine = engine or RunEngine(config, taskstore, bus=bus)
    shape_engine = shape_engine or ShapeEngine(config, taskstore, bus=bus)
    jobs = JobRunner(config, shape_engine)
    builder = ProjectBuilder(config, taskstore, engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = db.connect(config.state_dir)
        try:
            db.reconcile_runs(conn)
        finally:
            conn.close()
        yield
        jobs.stop()

    app = FastAPI(
        title="Command Center",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.taskstore = taskstore
    app.state.engine = engine
    app.state.shape_engine = shape_engine
    app.state.jobs = jobs

    app.state.bus = bus
    app.state.token = token

    pin = config.daemon.pin
    sessions: dict[str, float] = {}
    failures: dict[str, list[float]] = {}
    session_lock = threading.Lock()
    session_ttl = 7 * 24 * 60 * 60
    pin_window = 60.0
    pin_max_failures = 5

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        supplied = (
            header.removeprefix("Bearer ").strip()
            if header.startswith("Bearer ")
            else request.query_params.get("token")
        )
        if supplied == request.app.state.token:
            return
        now = time.time()
        with session_lock:
            expiry = sessions.get(supplied or "")
            if expiry is not None and expiry > now:
                return
            if expiry is not None:
                sessions.pop(supplied or "", None)
        raise HTTPException(status_code=401, detail="unauthorized")

    @app.post("/api/auth/pin")
    def api_auth_pin(payload: PinRequest, request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        now = time.time()
        with session_lock:
            recent = [stamp for stamp in failures.get(client, []) if now - stamp < pin_window]
            failures[client] = recent
            if len(recent) >= pin_max_failures:
                raise HTTPException(status_code=429, detail="too many PIN attempts")
        if pin is None:
            raise HTTPException(status_code=404, detail="PIN login is disabled")
        if not secrets.compare_digest(payload.pin, pin):
            with session_lock:
                failures.setdefault(client, []).append(now)
            raise HTTPException(status_code=401, detail="wrong PIN")
        session = secrets.token_urlsafe(24)
        expiry = now + session_ttl
        with session_lock:
            failures.pop(client, None)
            sessions[session] = expiry
        return {"token": session, "expires_in": session_ttl}

    auth = [Depends(require_token)]

    def connection():
        return db.connect(config.state_dir)

    @app.exception_handler(CommandCenterError)
    async def _command_error(_request: Request, exc: CommandCenterError):
        status = (
            409
            if isinstance(exc, (RunError, FocusError, AgendaError, ShapeError))
            else 400
        )
        payload: dict[str, Any] = {"detail": str(exc)}
        if isinstance(exc, AgendaValidationError):
            payload["issues"] = [
                {"item_id": issue.item_id, "code": issue.code, "detail": issue.detail}
                for issue in exc.issues
            ]
        if isinstance(exc, TaskStoreError):
            status = exc.status_code or 502
            if exc.payload:
                payload.update(exc.payload)
        return _json(payload, status)

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return {"ok": True, "version": "p5"}

    @app.get("/api/status", dependencies=auth)
    def api_status() -> dict[str, Any]:
        state = taskstore.state()
        conn = connection()
        try:
            runs = db.list_runs(conn, limit=20)
        finally:
            conn.close()
        return {
            "projects": len(state.projects),
            "open_tasks": sum(
                1
                for project in state.projects
                for objective in project.objectives
                for task in objective.tasks
                if task.status == "open"
            ),
            "runs": runs,
        }

    @app.get("/api/tasks", dependencies=auth)
    def api_tasks(project: str | None = None) -> dict[str, Any]:
        conn = connection()
        try:
            checks = EligibilityService(conn, taskstore, config).check_all()
        finally:
            conn.close()
        selected = [
            check for check in checks if project is None or check.project.id == project
        ]
        return {"tasks": [_task_check(check) for check in selected]}

    @app.put("/api/settings/roles", dependencies=auth)
    def api_set_roles(payload: RolesPatch) -> dict[str, Any]:
        updates = {
            key: value
            for key, value in payload.model_dump().items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=422, detail="nothing to update")
        try:
            save_roles(config, updates)
        except CommandCenterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"roles": dict(config.roles)}

    @app.get("/api/models", dependencies=auth)
    def api_models() -> dict[str, Any]:
        return {
            "models": [
                {
                    "id": status.id,
                    "label": status.label,
                    "model": status.model,
                    "up": status.up,
                    "startable": status.startable,
                    "reason": status.reason,
                    "context_window": (
                        config.models[status.id].context_window
                        if status.id in config.models
                        else 0
                    ),
                }
                for status in ModelProbe(config).check_all()
            ]
        }

    @app.post("/api/projects", status_code=201, dependencies=auth)
    def api_create_project(payload: ProjectCreateRequest) -> dict[str, Any]:
        project = taskstore.create_project(
            {"name": payload.name.strip(), "path": payload.path.strip()}
        )
        return {"project": project.model_dump(mode="json")}

    @app.post(
        "/api/projects/{project_id}/objectives",
        status_code=201,
        dependencies=auth,
    )
    def api_create_objective(
        project_id: str, payload: ObjectiveCreateRequest
    ) -> dict[str, Any]:
        objective = taskstore.create_objective(
            project_id, {"title": payload.title.strip()}
        )
        return {"objective": objective.model_dump(mode="json")}

    @app.post("/api/tasks", status_code=201, dependencies=auth)
    def api_create_task(payload: TaskCreateRequest) -> dict[str, Any]:
        task = taskstore.create_task(
            payload.project,
            payload.objective,
            {"title": payload.title.strip()},
        )
        result: dict[str, Any] = {"task": task.model_dump(mode="json")}
        if not payload.add_to_today:
            return result
        day = payload.day or date.today()
        agenda = taskstore.get_agenda(day)
        if agenda is None:
            raise HTTPException(status_code=409, detail="no agenda for that day")
        if payload.if_rev is not None and payload.if_rev != agenda.rev:
            return _json(
                {
                    "detail": "agenda revision changed",
                    "task": result["task"],
                    "current_rev": agenda.rev,
                },
                409,
            )
        if agenda.status == "locked":
            return _json(
                {
                    "detail": f"task {task.id} created, but the agenda is locked",
                    "task": result["task"],
                },
                409,
            )
        updated = taskstore.add_agenda_item(
            day,
            {
                "kind": "manual",
                "project": payload.project,
                "objective": payload.objective,
                "task": task.id,
                "reason": payload.title.strip(),
                "if_rev": agenda.rev,
            },
        )
        result["agenda"] = updated.model_dump(mode="json")
        return result

    @app.get("/api/projects", dependencies=auth)
    def api_projects() -> dict[str, Any]:
        state = taskstore.state()
        return {
            "projects": [
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status,
                    "focus_order": project.focus_order,
                    "spec_phase": (
                        project.spec_state.phase if project.spec_state else None
                    ),
                    "objectives": [
                        {
                            "id": objective.id,
                            "title": objective.title,
                            "tasks": len(objective.tasks),
                        }
                        for objective in project.objectives
                    ],
                }
                for project in state.projects
            ]
        }

    @app.get("/api/settings", dependencies=auth)
    def api_settings() -> dict[str, Any]:
        return {
            "version": "p5",
            "daemon": {"bind": config.daemon.bind, "port": config.daemon.port},
            "taskstore": {"url": config.taskstore.url},
            "paths": {
                "state_dir": str(config.state_dir),
                "worktree_root": str(config.worktree_root),
                "token_path": str(config.token_path),
                "token_exists": config.token_path.is_file(),
            },
            "opencode": {
                "binary": config.opencode.binary,
                "agents_dir": str(Path(config.opencode.agents_dir).expanduser()),
            },
            "local": {"start_script": config.local.start_script},
            "budgets": {
                "max_attempts": config.budgets.max_attempts,
                "gate_timeout_seconds": config.budgets.gate_timeout_seconds,
            },
            "policy": {
                "max_files": config.policy.max_files,
                "max_diff_bytes": config.policy.max_diff_bytes,
            },
            "roles": dict(config.roles),
            "autonomy": "L2 task landing",
        }

    @app.get("/api/overview", dependencies=auth)
    def api_overview() -> dict[str, Any]:
        conn = db.connect(config.state_dir)
        try:
            return {"rows": overview.build_overview(taskstore, conn, config)}
        finally:
            conn.close()

    @app.get("/api/agendas/{day}", dependencies=auth)
    def api_get_agenda(day: date) -> dict[str, Any]:
        agenda = taskstore.get_agenda(day)
        if agenda is None:
            raise HTTPException(status_code=404, detail="no agenda")
        return agenda.model_dump(mode="json")

    @app.post("/api/agendas/{day}/suggest", dependencies=auth)
    def api_suggest_agenda(
        day: date, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        agenda = taskstore.suggest_agenda(
            day,
            replace=bool(payload.get("replace", False)),
            model=payload.get("model"),
            if_rev=payload.get("if_rev"),
        )
        return agenda.model_dump(mode="json")

    @app.put("/api/agendas/{day}", dependencies=auth)
    def api_put_agenda(
        day: date, payload: dict[str, Any]
    ) -> dict[str, Any]:
        agenda = taskstore.put_agenda(day, payload)
        return agenda.model_dump(mode="json")

    @app.post("/api/agendas/{day}/tasks", dependencies=auth)
    def api_add_agenda_tasks(
        day: date, payload: AgendaAddRequest
    ) -> dict[str, Any]:
        """Add a batch of tasks to the agenda — one project or objective at a time."""
        agenda = taskstore.get_agenda(day)
        if agenda is None:
            raise HTTPException(status_code=404, detail="no agenda")
        if agenda.status == "locked":
            raise HTTPException(status_code=409, detail="agenda is locked")
        if payload.if_rev is not None and payload.if_rev != agenda.rev:
            return _json(
                {
                    "detail": "agenda revision changed",
                    "current_rev": agenda.rev,
                },
                409,
            )
        known = {
            task.id
            for project in taskstore.state().projects
            for objective in project.objectives
            for task in objective.tasks
        }
        existing = {item.task for item in agenda.items}
        added = 0
        skipped = 0
        for ref in payload.tasks:
            if ref.task in existing or ref.task not in known:
                skipped += 1
                continue
            agenda = taskstore.add_agenda_item(
                day,
                {
                    "kind": "manual",
                    "project": ref.project,
                    "objective": ref.objective,
                    "task": ref.task,
                    "reason": "",
                    "if_rev": agenda.rev,
                },
            )
            existing.add(ref.task)
            added += 1
        return {
            "agenda": agenda.model_dump(mode="json"),
            "added": added,
            "skipped": skipped,
        }

    @app.post("/api/agendas/{day}/quick-add", dependencies=auth)
    def api_quick_add(
        day: date, payload: QuickAddRequest
    ) -> dict[str, Any]:
        agenda = taskstore.get_agenda(day)
        if agenda is None:
            raise HTTPException(status_code=404, detail="no agenda")
        if agenda.status == "locked":
            raise HTTPException(status_code=409, detail="agenda is locked")
        if payload.if_rev is not None and payload.if_rev != agenda.rev:
            return _json(
                {
                    "detail": "agenda revision changed",
                    "current_rev": agenda.rev,
                },
                409,
            )
        try:
            task = taskstore.create_task(
                payload.project,
                "o_general",
                {"title": payload.title},
            )
        except TaskStoreError as exc:
            if exc.status_code == 404:
                return _json(
                    {
                        "detail": (
                            f"project {payload.project!r} has no o_general objective"
                        )
                    },
                    422,
                )
            raise
        item = {
            "kind": "manual",
            "project": payload.project,
            "objective": "o_general",
            "task": task.id,
            "reason": payload.reason or payload.title,
            "due_time": payload.due_time,
            "if_rev": payload.if_rev if payload.if_rev is not None else agenda.rev,
        }
        try:
            updated = taskstore.add_agenda_item(day, item)
        except TaskStoreError as exc:
            return _json(
                {
                    "detail": f"task {task.id} created, but agenda item failed: {exc}",
                    "task_id": task.id,
                    "task": task.model_dump(mode="json"),
                    "current_rev": exc.payload.get("current_rev")
                    if exc.payload
                    else None,
                },
                exc.status_code or 409,
            )
        return {
            "task": task.model_dump(mode="json"),
            "agenda": updated.model_dump(mode="json"),
        }

    @app.get("/api/focus", dependencies=auth)
    def api_focus() -> dict[str, Any]:
        conn = connection()
        try:
            state = FocusService(conn, taskstore).list()
        finally:
            conn.close()
        return {
            "entries": [
                {
                    "project": entry.project_id,
                    "position": entry.position,
                    "set_at": entry.set_at,
                }
                for entry in state.entries
            ],
            "projects": [
                {"id": project.id, "name": project.name}
                for project in state.projects
            ],
            "stale": state.stale,
            "tag_errors": state.tag_errors,
        }

    @app.put("/api/focus", dependencies=auth)
    def api_set_focus(payload: FocusRequest) -> dict[str, Any]:
        conn = connection()
        try:
            state = FocusService(conn, taskstore).set(payload.projects)
        finally:
            conn.close()
        return {
            "entries": [
                {"project": entry.project_id, "position": entry.position}
                for entry in state.entries
            ],
            "tag_errors": state.tag_errors,
        }

    @app.post("/api/agendas/{day}/lock", dependencies=auth)
    def api_lock_agenda(
        day: date, request: Request, payload: LockRequest | None = None
    ) -> dict[str, Any]:
        conn = connection()
        try:
            service = AgendaService(
                conn, taskstore, config, EligibilityService(conn, taskstore, config)
            )
            result = service.lock(day, payload.if_rev if payload else None)
        finally:
            conn.close()
        request.app.state.bus.publish(
            "agenda.locked", {"date": day.isoformat(), "rev": result.agenda.rev}
        )
        return {
            "agenda": result.agenda.model_dump(mode="json"),
            "warnings": [_reason(warning) for warning in result.warnings],
            "already_locked": result.already_locked,
        }

    @app.post("/api/agendas/{day}/unlock", dependencies=auth)
    def api_unlock_agenda(
        day: date, payload: UnlockRequest, request: Request
    ) -> dict[str, Any]:
        conn = connection()
        try:
            service = AgendaService(
                conn, taskstore, config, EligibilityService(conn, taskstore, config)
            )
            agenda = service.unlock(day, payload.reason)
        finally:
            conn.close()
        request.app.state.bus.publish(
            "agenda.unlocked",
            {
                "date": day.isoformat(),
                "rev": agenda.rev,
                "reason": payload.reason,
            },
        )
        return agenda.model_dump(mode="json")

    @app.get("/api/agendas/{day}/snapshot", dependencies=auth)
    def api_snapshot(day: date) -> dict[str, Any]:
        conn = connection()
        try:
            snapshot = AgendaService(
                conn, taskstore, config, EligibilityService(conn, taskstore, config)
            ).snapshot(day)
        finally:
            conn.close()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="no snapshot")
        return snapshot

    @app.post("/api/runs", status_code=201, dependencies=auth)
    def api_start_run(payload: RunRequest) -> dict[str, Any]:
        record = engine.create(payload.task, payload.model, payload.reviewer)
        conn = connection()
        try:
            db.save_run(conn, record.model_dump(mode="json"))
        finally:
            conn.close()
        thread = threading.Thread(
            target=engine.execute, args=(record,), daemon=True
        )
        thread.start()
        return record.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/build", dependencies=auth)
    def api_project_build(project_id: str) -> dict[str, Any]:
        return builder.status(project_id)

    @app.post(
        "/api/projects/{project_id}/build",
        status_code=202,
        dependencies=auth,
    )
    def api_project_build_start(
        project_id: str, payload: ProjectBuildRequest | None = None
    ) -> dict[str, Any]:
        request = payload or ProjectBuildRequest()
        return builder.start(project_id, request.model, request.reviewer)

    @app.post(
        "/api/projects/{project_id}/build/stop", dependencies=auth
    )
    def api_project_build_stop(project_id: str) -> dict[str, Any]:
        return builder.stop(project_id)

    @app.get("/api/runs", dependencies=auth)
    def api_runs(
        project: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        conn = connection()
        try:
            return db.list_runs(conn, project=project, state=state)
        finally:
            conn.close()

    @app.get("/api/runs/{run_id}", dependencies=auth)
    def api_run(run_id: str) -> dict[str, Any]:
        conn = connection()
        try:
            run = db.get_run(conn, run_id)
        finally:
            conn.close()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/api/runs/{run_id}/artifacts/{kind}", dependencies=auth)
    def api_artifact(run_id: str, kind: str) -> Response:
        conn = connection()
        try:
            run = db.get_run(conn, run_id)
        finally:
            conn.close()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if kind in {"gate", "policy", "review"}:
            path = run.get(f"{'reviewer' if kind == 'review' else kind}_path")
        elif kind in {"diff", "prompt", "agent", "stderr", "changes", "run"}:
            names = {
                "diff": "diff.patch",
                "prompt": "prompt.txt",
                "agent": "agent.jsonl",
                "stderr": "agent.stderr.log",
                "changes": "CHANGES.md",
                "run": "run.json",
            }
            path = str(Path(run.get("run_dir", "")) / names[kind])
        else:
            raise HTTPException(status_code=404, detail="unknown artifact")
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        media = (
            "application/json"
            if kind in {"gate", "policy", "review", "run"}
            else "text/plain"
        )
        return Response(text, media_type=media)

    @app.get("/api/runs/{run_id}/logs", dependencies=auth)
    def api_run_logs(
        run_id: str,
        stream: str = "agent",
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        conn = connection()
        try:
            run = db.get_run(conn, run_id)
        finally:
            conn.close()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if stream not in {"agent", "stderr"}:
            raise HTTPException(status_code=404, detail="unknown log stream")
        filename = "agent.jsonl" if stream == "agent" else "agent.stderr.log"
        path = Path(run.get("run_dir", "")) / filename
        data = path.read_bytes() if path.is_file() else b""
        chunk = data[offset : offset + 256 * 1024]
        new_offset = offset + len(chunk)
        terminal = run.get("state") in {
            "merged",
            "blocked",
            "error",
            "cancelled",
        }
        return {
            "stream": stream,
            "offset": new_offset,
            "text": chunk.decode("utf-8", errors="replace"),
            "eof": terminal and new_offset >= len(data),
            "state": run.get("state"),
            "attempt": run.get("attempt"),
        }

    @app.post("/api/runs/{run_id}/cancel", dependencies=auth)
    def api_cancel(run_id: str) -> dict[str, Any]:
        conn = connection()
        try:
            if db.get_run(conn, run_id) is None:
                raise HTTPException(status_code=404, detail="run not found")
            db.request_cancel(conn, run_id)
        finally:
            conn.close()
        return {"ok": True, "run_id": run_id}

    @app.post("/api/shapes", status_code=201, dependencies=auth)
    def api_shape_start(payload: ShapeStartRequest) -> dict[str, Any]:
        record = shape_engine.prepare(
            payload.project,
            payload.rewrite,
            objective_id=payload.objective,
            task_id=payload.task,
            model=payload.model,
        )
        jobs.enqueue(
            shape=record.id,
            project=payload.project,
            kind="draft",
            payload={},
        )
        return record.model_dump(mode="json")

    @app.get("/api/shapes/{project_id}", dependencies=auth)
    def api_shape_status(project_id: str) -> dict[str, Any]:
        status = shape_engine.status(project_id, include_latest=True)
        if status.get("shape") is None:
            raise HTTPException(status_code=404, detail="no shape")
        return status

    @app.patch("/api/shapes/{project_id}", dependencies=auth)
    def api_shape_model(
        project_id: str, payload: ShapeModelRequest
    ) -> dict[str, Any]:
        return shape_engine.set_model(project_id, payload.model)

    @app.post(
        "/api/shapes/{project_id}/jobs",
        status_code=201,
        dependencies=auth,
    )
    def api_shape_job(
        project_id: str, payload: ShapeJobRequest
    ) -> dict[str, Any]:
        if payload.kind not in JOB_KINDS:
            raise HTTPException(
                status_code=422, detail=f"unknown job kind {payload.kind!r}"
            )
        status = shape_engine.status(project_id)
        shape = status.get("shape")
        if shape is None:
            raise HTTPException(status_code=404, detail="no active shape")
        if payload.kind == "discuss" and not (payload.message or "").strip():
            raise HTTPException(status_code=422, detail="message is required")
        if payload.kind == "answers":
            answers = {
                key: value.strip()
                for key, value in (payload.answers or {}).items()
                if value.strip()
            }
            if not answers:
                raise HTTPException(status_code=422, detail="no answers given")
            built: dict[str, Any] = {
                "answers": [
                    {"id": key, "value": value} for key, value in answers.items()
                ]
            }
        elif payload.kind == "discuss":
            built = {
                "message": (payload.message or "").strip(),
                "question_id": payload.question_id,
            }
        else:
            built = {
                "scope": payload.scope or "all",
                "instruction": (payload.message or "").strip() or None,
            }
        return jobs.enqueue(
            shape=shape["id"],
            project=project_id,
            kind=payload.kind,
            payload=built,
        )

    @app.get("/api/shapes/{project_id}/jobs", dependencies=auth)
    def api_shape_jobs(project_id: str) -> dict[str, Any]:
        status = shape_engine.status(project_id)
        shape = status.get("shape")
        if shape is None:
            return {"jobs": []}
        return {"jobs": jobs.list(shape["id"])}

    @app.post(
        "/api/shapes/{project_id}/jobs/{job_id}/cancel", dependencies=auth
    )
    def api_shape_job_cancel(project_id: str, job_id: str) -> dict[str, Any]:
        if not jobs.cancel(job_id):
            raise HTTPException(status_code=409, detail="job is not queued")
        return {"ok": True, "job": job_id}

    @app.get("/api/shapes/{project_id}/tree", dependencies=auth)
    def api_shape_tree(project_id: str) -> dict[str, Any]:
        if shape_engine.status(project_id).get("shape") is None:
            raise HTTPException(status_code=404, detail="no active shape")
        return shape_engine.tree(project_id)

    @app.put("/api/shapes/{project_id}/tree", dependencies=auth)
    def api_save_shape_tree(
        project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return shape_engine.save_tree(project_id, payload)

    @app.get("/api/shapes/{project_id}/use-cases", dependencies=auth)
    def api_shape_use_cases(project_id: str) -> dict[str, Any]:
        return shape_engine.use_cases(project_id)

    @app.put("/api/shapes/{project_id}/use-cases", dependencies=auth)
    def api_save_shape_use_cases(
        project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return shape_engine.save_use_cases(project_id, payload)

    @app.get("/api/shapes/{project_id}/c4", dependencies=auth)
    def api_shape_c4(project_id: str) -> dict[str, Any]:
        return shape_engine.c4(project_id)

    @app.put("/api/shapes/{project_id}/c4", dependencies=auth)
    def api_save_shape_c4(
        project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return shape_engine.save_c4(project_id, payload)

    @app.get("/api/projects/{project_id}/spec", dependencies=auth)
    def api_project_spec(
        project_id: str, ref: str | None = None
    ) -> dict[str, Any]:
        """The approved spec for a project, read from the ref builds branch from."""
        try:
            project = taskstore.get_project(project_id)
        except TaskStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        root = project_root(project.path)
        if root is None or not gitutil.is_git_repo(root):
            raise HTTPException(status_code=404, detail="project has no checkout")
        candidates = [ref] if ref else []
        candidates += ["cc/integration", "master"]
        chosen = "HEAD"
        for candidate in candidates:
            if candidate and gitutil.branch_exists(root, candidate):
                chosen = candidate
                break
        files: list[dict[str, Any]] = []
        # The ref is authoritative, so ask for the canonical paths rather than
        # letting the working tree decide (it may still carry a legacy SPEC.md).
        for doc in ("docs/spec/SPEC.md", "docs/spec/DESIGN.md"):
            try:
                text = gitutil.blob(root, chosen, doc).decode("utf-8", "replace")
            except gitutil.GitError:
                continue
            files.append({"path": doc, "name": Path(doc).name, "text": text})
        if not files:
            for doc in ("SPEC.md", "DESIGN.md"):
                try:
                    text = gitutil.blob(root, chosen, doc).decode("utf-8", "replace")
                except gitutil.GitError:
                    continue
                files.append({"path": doc, "name": Path(doc).name, "text": text})
        errors: list[str] = []
        use_cases: dict[str, Any] | None = None
        c4: dict[str, Any] | None = None
        for doc, parser, alias in (
            (USE_CASES_DOC, parse_use_cases, False),
            (C4_DOC, parse_c4, True),
        ):
            try:
                text = gitutil.blob(root, chosen, doc).decode("utf-8", "replace")
            except gitutil.GitError:
                continue
            try:
                parsed = parser(text, f"{chosen}:{doc}")
            except ShapeError as exc:
                errors.append(str(exc))
                continue
            payload = parsed.model_dump(mode="json", by_alias=alias)
            if doc == C4_DOC:
                c4 = payload
            else:
                use_cases = payload
        return {
            "project": project_id,
            "ref": chosen,
            "files": files,
            "use_cases": use_cases,
            "c4": c4,
            "errors": errors,
        }

    @app.get("/api/shapes/{project_id}/spec", dependencies=auth)
    def api_shape_spec(project_id: str) -> dict[str, Any]:
        status = shape_engine.status(project_id)
        shape = status.get("shape")
        if shape is None:
            raise HTTPException(status_code=404, detail="no active shape")
        root = Path(shape["worktree"])
        if not root.is_dir():
            raise HTTPException(status_code=404, detail="the shape worktree is gone")
        files = []
        for doc in default_docs(root):
            path = root / doc
            if path.is_file():
                files.append(
                    {
                        "path": doc,
                        "name": path.name,
                        "text": path.read_text(encoding="utf-8"),
                    }
                )
        return {"shape": shape["id"], "files": files}

    @app.post("/api/shapes/{project_id}/check", dependencies=auth)
    def api_shape_check(project_id: str) -> dict[str, Any]:
        record, check = shape_engine.check(project_id)
        return {
            "shape": record.model_dump(mode="json"),
            "check": check.to_dict(),
        }

    @app.post("/api/shapes/{project_id}/materialize", dependencies=auth)
    def api_shape_materialize(project_id: str) -> dict[str, Any]:
        return shape_engine.materialize(project_id).model_dump(mode="json")

    @app.post("/api/shapes/{project_id}/approve", dependencies=auth)
    def api_shape_approve(project_id: str) -> dict[str, Any]:
        return shape_engine.approve(project_id).model_dump(mode="json")

    @app.post("/api/shapes/{project_id}/cancel", dependencies=auth)
    def api_shape_cancel(project_id: str) -> dict[str, Any]:
        return shape_engine.cancel(project_id).model_dump(mode="json")

    @app.get("/api/shapes/{project_id}/diff", dependencies=auth)
    def api_shape_diff(project_id: str) -> dict[str, Any]:
        return {"project": project_id, "patch": shape_engine.diff(project_id)}

    @app.get("/api/events", dependencies=auth)
    async def api_events(request: Request) -> StreamingResponse:
        raw_last = request.headers.get("last-event-id") or request.query_params.get(
            "last_event_id"
        )
        after_id = int(raw_last) if raw_last and raw_last.isdigit() else None

        async def stream():
            subscriber = request.app.state.bus.subscribe(after_id)
            try:
                while True:
                    try:
                        event = await asyncio.to_thread(subscriber.get, True, 15)
                    except queue.Empty:
                        yield ": ping\n\n"
                        continue
                    yield (
                        f"id: {event['id']}\n"
                        f"event: {event['event']}\n"
                        f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                    )
            finally:
                request.app.state.bus.unsubscribe(subscriber)

        return StreamingResponse(stream(), media_type="text/event-stream")

    web_dir = REPO_ROOT / "web"
    if (web_dir / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

    return app


def _json(payload: dict[str, Any], status: int):
    from fastapi.responses import JSONResponse

    return JSONResponse(payload, status_code=status)


def main() -> None:
    config = load_config()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.daemon.bind,
        port=config.daemon.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
