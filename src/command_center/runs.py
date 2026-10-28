from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from . import db


class RunState(StrEnum):
    queued = "queued"
    running = "running"
    gate_failed = "gate_failed"
    policy_failed = "policy_failed"
    review_changes = "review_changes"
    merging = "merging"
    merged = "merged"
    blocked = "blocked"
    error = "error"
    cancelled = "cancelled"


class RunRecord(BaseModel):
    id: str
    kind: str = "build"
    project: str
    objective: str
    task: str
    agenda_item: str | None = None
    attempt: int = 1
    model_requested: str = ""
    model_resolved: str = ""
    provider: str = ""
    reviewer_model: str = ""
    reviewer_resolved: str = ""
    reviewer_session_id: str | None = None
    reviewer_verdict: str | None = None
    reviewer_path: str = ""
    base_sha: str = ""
    branch: str = ""
    worktree: str = ""
    run_dir: str = ""
    session_id: str | None = None
    state: RunState = RunState.queued
    verdict: str | None = None
    gate_path: str = ""
    diff_path: str = ""
    policy_path: str = ""
    policy_passed: bool | None = None
    merge_sha: str | None = None
    error: str | None = None
    notes: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created: datetime
    updated: datetime

    def save(self, conn: sqlite3.Connection) -> None:
        self.updated = datetime.now(UTC)
        payload = self.model_dump(mode="json")
        db.save_run(conn, payload)
        if self.run_dir:
            try:
                run_dir = Path(self.run_dir)
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "run.json").write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass

    @classmethod
    def load(cls, conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
        data = db.get_run(conn, run_id)
        return cls.model_validate(data) if data else None

    @classmethod
    def new(cls, **kwargs: object) -> RunRecord:
        now = datetime.now(UTC)
        return cls(
            id=f"r_{secrets.token_hex(4)}",
            created=now,
            updated=now,
            **kwargs,
        )
