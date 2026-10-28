from __future__ import annotations

import threading
from typing import Any

from . import db
from .config import Config
from .errors import CommandCenterError
from .shape import ShapeEngine, ShapeRecord

KINDS = {"draft", "discuss", "answers", "revise", "workflows", "c4"}
MAX_ATTEMPTS = 2
TRANSIENT = (
    "econnreset",
    "econnrefused",
    "socket",
    "transport",
    "fetch failed",
    "network",
    "overloaded",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
)


def is_transient(error: str | None) -> bool:
    """A dropped connection is worth one retry; a bad prompt is not."""
    if not error:
        return False
    lowered = error.lower()
    return any(token in lowered for token in TRANSIENT)


def job_title(kind: str, payload: dict[str, Any]) -> str:
    if kind == "answers":
        ids = [row.get("id") for row in payload.get("answers", [])]
        if len(ids) == 1:
            return f"answer {ids[0]}"
        if ids:
            return f"answer {ids[0]} +{len(ids) - 1}"
        return "answer"
    if kind == "discuss":
        focus = payload.get("question_id")
        return f"discuss {focus}" if focus else "discuss"
    if kind == "workflows":
        return "use cases"
    if kind == "c4":
        return "C4 model"
    if kind == "draft":
        return "draft"
    if kind == "revise":
        scope = payload.get("scope")
        return f"revise {scope}" if scope and scope != "all" else "revise"
    return kind


class JobRunner:
    """One consumer per daemon: strictly FIFO, one turn at a time.

    Jobs are durable in SQLite. A job that is `running` when the daemon dies is
    requeued at startup, so a crash never strands a shape behind a phantom turn.
    """

    def __init__(
        self,
        config: Config,
        engine: ShapeEngine,
        *,
        poll_seconds: float = 0.5,
        autostart: bool = True,
    ):
        self.config = config
        self.engine = engine
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._autostart = autostart

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            conn = db.connect(self.config.state_dir)
            try:
                db.requeue_running_jobs(conn)
            finally:
                conn.close()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="cc-jobs", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        terminate = getattr(self.engine, "terminate", None)
        if callable(terminate):
            terminate()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    # ---------------------------------------------------------------- public
    def enqueue(
        self,
        *,
        shape: str,
        project: str,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise CommandCenterError(f"unknown job kind {kind!r}")
        conn = db.connect(self.config.state_dir)
        try:
            job = db.create_job(
                conn,
                shape=shape,
                project=project,
                kind=kind,
                title=job_title(kind, payload),
                payload=payload,
            )
        finally:
            conn.close()
        if self._autostart:
            self.start()
        return job

    def list(self, shape: str) -> list[dict[str, Any]]:
        conn = db.connect(self.config.state_dir)
        try:
            return db.list_jobs(conn, shape)
        finally:
            conn.close()

    def cancel(self, job_id: str) -> bool:
        conn = db.connect(self.config.state_dir)
        try:
            return db.cancel_job(conn, job_id)
        finally:
            conn.close()

    # ---------------------------------------------------------------- worker
    def _loop(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._tick()
            except Exception:  # noqa: BLE001 - the worker must not die
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)

    def _tick(self) -> bool:
        conn = db.connect(self.config.state_dir)
        try:
            job = db.next_job(conn)
            if job is None:
                return False
            if not db.claim_job(conn, job["id"]):
                return False
            self._run(conn, job)
            return True
        finally:
            conn.close()

    def _run(self, conn, job: dict[str, Any]) -> None:
        kind = job["kind"]
        payload = job.get("payload") or {}
        reply: str | None = None
        usage: dict[str, Any] = {}
        error: str | None = None
        try:
            if kind == "draft":
                record = self._active(job["project"])
                if record is None:
                    raise CommandCenterError("no active shape")
                record = self.engine.draft(record)
                reply = record.last_reply
                usage = record.last_usage or {}
            elif kind == "discuss":
                result = self.engine.discuss(
                    job["project"],
                    str(payload.get("message") or ""),
                    payload.get("question_id"),
                )
                reply = result["reply"]
                usage = result
            elif kind == "c4":
                record = self._active(job["project"])
                if record is None:
                    raise CommandCenterError("no active shape")
                record = self.engine.derive_c4(record)
                reply = record.last_reply
                usage = record.last_usage or {}
            elif kind == "workflows":
                record = self._active(job["project"])
                if record is None:
                    raise CommandCenterError("no active shape")
                record = self.engine.derive_use_cases(record)
                reply = record.last_reply
                usage = record.last_usage or {}
            elif kind in {"answers", "revise"}:
                if kind == "answers":
                    for answer in payload.get("answers", []):
                        self.engine.answer(
                            job["project"],
                            answer["id"],
                            answer.get("value") or "",
                            in_turn=True,
                        )
                record = self._active(job["project"])
                if record is None:
                    raise CommandCenterError("no active shape")
                record = self.engine.revise(
                    record, payload.get("instruction") if kind == "revise" else None
                )
                reply = record.last_reply
                usage = record.last_usage or {}
            else:
                raise CommandCenterError(f"unknown job kind {kind!r}")
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            error = str(exc)
        if error is not None:
            fresh = db.get_job(conn, job["id"])
            attempts = int((fresh or {}).get("attempts") or 1)
            if is_transient(error) and attempts < MAX_ATTEMPTS:
                db.requeue_job(conn, job["id"], error)
                return
        db.finish_job(
            conn,
            job["id"],
            status="done" if error is None else "failed",
            reply=reply,
            error=error,
            tokens_in=int(usage.get("tokens_in") or 0),
            tokens_out=int(usage.get("tokens_out") or 0),
            cost=float(usage.get("cost") or 0.0),
            context=int(usage.get("context") or 0),
        )

    def _active(self, project: str) -> ShapeRecord | None:
        status = self.engine.status(project)
        shape = status.get("shape")
        return ShapeRecord.model_validate(shape) if shape else None
