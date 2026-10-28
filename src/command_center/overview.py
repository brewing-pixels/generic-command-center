from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import db
from .config import Config
from .eligibility import EligibilityService, task_status_map
from .errors import ShapeError
from .shape import ShapeRecord, load_questions
from .taskstore import TaskStoreClient, TaskStoreProject

SEVERITY_RANK = {"high": 0, "normal": 1, "info": 2}
BLOCKED_RUN_STATES = {"blocked", "error"}
RUN_ROW_LIMIT = 5
RUN_AGE_DAYS = 7


def _row(
    *,
    row_id: str,
    kind: str,
    severity: str,
    title: str,
    detail: str = "",
    actions: list[dict[str, str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "id": row_id,
        "kind": kind,
        "severity": severity,
        "title": title,
        "detail": detail,
        "actions": actions or [],
    }
    payload.update(extra)
    return payload


def _questions(record: ShapeRecord) -> list[dict[str, Any]]:
    try:
        questions = load_questions(Path(record.worktree))
    except ShapeError:
        return []
    return [
        {
            "id": question.id,
            "question": question.question,
            "default": question.default,
            "answer": question.answer or "",
        }
        for question in questions.questions
    ]


def _shape_row(project: TaskStoreProject, record: ShapeRecord) -> dict[str, Any]:
    base = {
        "project": project.id,
        "project_name": project.name,
        "shape": record.id,
        "shape_state": record.state,
    }
    if record.state in {"drafting", "revising"}:
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="info",
            title=f"{project.name}: inquisitor working",
            detail="Drafting or revising. Nothing needed from you.",
            **base,
        )
    if record.state in {"questions", "answers_ready"}:
        questions = _questions(record)
        open_questions = [item for item in questions if not item["answer"].strip()]
        if not open_questions:
            return _row(
                row_id=f"shape:{project.id}",
                kind="shape",
                severity="normal",
                title=f"{project.name}: answers saved, revision pending",
                detail="Run the revision to fold the answers into the spec.",
                actions=[{"id": "revise", "label": "Revise"}],
                **base,
            )
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="high",
            title=f"{project.name}: {len(open_questions)} question"
            f"{'' if len(open_questions) == 1 else 's'} to answer",
            detail="The inquisitor needs a decision before it can revise.",
            actions=[{"id": "answer", "label": "Answer"}],
            questions=open_questions,
            **base,
        )
    if record.state == "check_failed":
        issues = (record.check or {}).get("issues") or []
        detail = issues[0] if issues else "the check failed"
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="high",
            title=f"{project.name}: shape check failed",
            detail=detail,
            actions=[{"id": "revise", "label": "Fix & revise"}],
            **base,
        )
    if record.state == "error":
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="high",
            title=f"{project.name}: inquisitor failed",
            detail=record.error or "the pass failed",
            actions=[{"id": "revise", "label": "Resume & revise"}],
            **base,
        )
    if record.state == "ready":
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="normal",
            title=f"{project.name}: shape ready",
            detail="Materialize it to create the TaskStore ids and the gate file.",
            actions=[{"id": "materialize", "label": "Materialize"}],
            **base,
        )
    if record.state == "materialized":
        return _row(
            row_id=f"shape:{project.id}",
            kind="shape",
            severity="normal",
            title=f"{project.name}: ready to approve",
            detail="Review the diff, then approve the spec.",
            actions=[{"id": "approve", "label": "Approve"}],
            **base,
        )
    return _row(
        row_id=f"shape:{project.id}",
        kind="shape",
        severity="info",
        title=f"{project.name}: shape {record.state}",
        detail="",
        **base,
    )


def _run_rows(
    conn: sqlite3.Connection, projects: dict[str, TaskStoreProject]
) -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=RUN_AGE_DAYS)).isoformat()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in db.list_runs(conn, limit=200):
        if run.get("state") not in BLOCKED_RUN_STATES:
            continue
        task = str(run.get("task") or "")
        if not task or task in seen:
            continue
        created = str(run.get("created") or "")
        if created[:10] < cutoff:
            continue
        seen.add(task)
        project = projects.get(str(run.get("project") or ""))
        code = run.get("verdict") or run.get("state")
        rows.append(
            _row(
                row_id=f"run:{run['id']}",
                kind="run",
                severity="high" if code in {"gate_failed", "error"} else "normal",
                title=f"{project.name if project else run.get('project')}: "
                f"{run.get('task')} {code}",
                detail=(run.get("notes") or ["no detail"])[-1],
                actions=[
                    {"id": "inspect", "label": "Inspect"},
                    {"id": "retry", "label": "Retry"},
                ],
                run=run["id"],
                task=task,
                project=str(run.get("project") or ""),
            )
        )
        if len(rows) >= RUN_ROW_LIMIT:
            break
    return rows


def _agenda_rows(
    taskstore: TaskStoreClient,
    conn: sqlite3.Connection,
    config: Config,
    projects: dict[str, TaskStoreProject],
) -> list[dict[str, Any]]:
    today = date.today()
    agenda = taskstore.get_agenda(today)
    if agenda is None or (agenda.status == "draft" and not agenda.items):
        return [
            _row(
                row_id="agenda:none",
                kind="agenda",
                severity="normal",
                title="Today has no agenda",
                detail="Compose the day before building.",
                actions=[{"id": "compose", "label": "Compose today"}],
            )
        ]
    rows: list[dict[str, Any]] = []
    if agenda.status == "draft":
        rows.append(
            _row(
                row_id="agenda:draft",
                kind="agenda",
                severity="normal",
                title=f"Today's agenda is a draft ({len(agenda.items)} items)",
                detail="Lock it to start building.",
                actions=[{"id": "lock", "label": "Lock today"}],
            )
        )
    statuses = task_status_map(list(projects.values()))
    eligibility = EligibilityService(conn, taskstore, config)
    for item in agenda.items:
        project = projects.get(item.project)
        if project is None:
            continue
        objective = next(
            (row for row in project.objectives if row.id == item.objective), None
        )
        if objective is None:
            continue
        task = next(
            (row for row in objective.tasks if row.id == item.task), None
        )
        if task is None or task.status == "done":
            continue
        check = eligibility.check_task_for(project, objective, task, statuses)
        if check.ready:
            continue
        codes = sorted({reason.code for reason in check.reasons})
        spec_debt = any(
            code in {"no_gate", "spec_unapproved", "spec_dirty", "spec_missing"}
            for code in codes
        )
        rows.append(
            _row(
                row_id=f"agenda:{item.id}",
                kind="agenda",
                severity="high" if spec_debt else "normal",
                title=f"{project.name}: {task.title} cannot build",
                detail=", ".join(codes) or "not eligible",
                actions=[{"id": "open", "label": "Open work item"}],
                task=task.id,
                project=project.id,
            )
        )
    return rows


def build_overview(
    taskstore: TaskStoreClient, conn: sqlite3.Connection, config: Config
) -> list[dict[str, Any]]:
    state = taskstore.state()
    projects = {project.id: project for project in state.projects}
    rows: list[dict[str, Any]] = []
    for project in state.projects:
        if project.status in {"parked", "archived"}:
            continue
        shape = db.active_shape_for_project(conn, project.id)
        if shape:
            rows.append(
                _shape_row(project, ShapeRecord.model_validate(shape))
            )
    rows.extend(_agenda_rows(taskstore, conn, config, projects))
    rows.extend(_run_rows(conn, projects))
    rows.sort(
        key=lambda row: (
            SEVERITY_RANK.get(row["severity"], 3),
            row["title"].lower(),
        )
    )
    return rows
