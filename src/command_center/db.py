from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS focus (
    project_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL UNIQUE CHECK (position BETWEEN 1 AND 3),
    set_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    task_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agenda_snapshots (
    date TEXT PRIMARY KEY,
    rev INTEGER NOT NULL,
    locked_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    objective TEXT,
    task TEXT NOT NULL,
    agenda_item TEXT,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    model_requested TEXT,
    model_resolved TEXT,
    provider TEXT,
    base_sha TEXT,
    branch TEXT,
    worktree TEXT,
    session_id TEXT,
    verdict TEXT,
    merge_sha TEXT,
    policy_passed INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS runs_project_state ON runs(project, state);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shapes (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    state TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shapes_project ON shapes(project, created DESC);

CREATE TABLE IF NOT EXISTS work_sessions (
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    author_session TEXT,
    critic_session TEXT,
    updated TEXT NOT NULL,
    PRIMARY KEY (project, task)
);

CREATE TABLE IF NOT EXISTS turn_jobs (
    id TEXT PRIMARY KEY,
    shape TEXT NOT NULL,
    project TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    position INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    reply TEXT,
    error TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    context INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT
);

CREATE INDEX IF NOT EXISTS turn_jobs_shape ON turn_jobs(shape, status, position);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(state_dir: Path) -> sqlite3.Connection:
    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "command-center.sqlite3"
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(turn_jobs)")
    }
    if columns and "attempts" not in columns:
        conn.execute(
            "ALTER TABLE turn_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


# ------------------------------------------------------------------ focus
def focus_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT project_id, position, set_at FROM focus ORDER BY position"
        )
    )


def set_focus(
    conn: sqlite3.Connection, project_ids: list[str], set_at: str | None = None
) -> None:
    stamp = set_at or now_iso()
    with conn:
        conn.execute("DELETE FROM focus")
        for position, project_id in enumerate(project_ids, start=1):
            conn.execute(
                "INSERT INTO focus (project_id, position, set_at) VALUES (?, ?, ?)",
                (project_id, position, stamp),
            )


def clear_focus(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM focus")


# ------------------------------------------------------------------ locks
def lock_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM locks WHERE task_id = ?", (task_id,)
        ).fetchone()
        is not None
    )


def acquire_lock(conn: sqlite3.Connection, task_id: str, owner: str) -> bool:
    stamp = now_iso()
    try:
        with conn:
            conn.execute(
                "INSERT INTO locks (task_id, owner, acquired_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, owner, stamp, stamp),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def release_lock(conn: sqlite3.Connection, task_id: str, owner: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM locks WHERE task_id = ? AND owner = ?", (task_id, owner)
        )


# ------------------------------------------------------------- snapshots
def get_snapshot(conn: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT date, rev, locked_at, payload FROM agenda_snapshots WHERE date = ?",
        (day,),
    ).fetchone()


def put_snapshot(
    conn: sqlite3.Connection,
    day: str,
    rev: int,
    locked_at: str,
    payload: dict[str, Any],
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO agenda_snapshots (date, rev, locked_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                rev = excluded.rev,
                locked_at = excluded.locked_at,
                payload = excluded.payload
            """,
            (day, rev, locked_at, json.dumps(payload, ensure_ascii=False)),
        )


def delete_snapshot(conn: sqlite3.Connection, day: str) -> None:
    with conn:
        conn.execute("DELETE FROM agenda_snapshots WHERE date = ?", (day,))


# ------------------------------------------------------------------ runs
def save_run(conn: sqlite3.Connection, run: dict[str, Any]) -> None:
    payload = json.dumps(run, ensure_ascii=False)
    values = (
        run["id"],
        run.get("kind", "build"),
        run.get("project"),
        run.get("objective"),
        run.get("task"),
        run.get("agenda_item"),
        run.get("state", "queued"),
        int(run.get("attempt", 1)),
        run.get("model_requested"),
        run.get("model_resolved"),
        run.get("provider"),
        run.get("base_sha"),
        run.get("branch"),
        run.get("worktree"),
        run.get("session_id"),
        run.get("verdict"),
        run.get("merge_sha"),
        None
        if run.get("policy_passed") is None
        else int(bool(run["policy_passed"])),
        int(bool(run.get("cancel_requested", False))),
        run.get("created"),
        run.get("updated"),
        payload,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO runs (
                id, kind, project, objective, task, agenda_item, state, attempt,
                model_requested, model_resolved, provider, base_sha, branch,
                worktree, session_id, verdict, merge_sha, policy_passed,
                cancel_requested, created, updated, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                attempt = excluded.attempt,
                model_requested = excluded.model_requested,
                model_resolved = excluded.model_resolved,
                provider = excluded.provider,
                base_sha = excluded.base_sha,
                branch = excluded.branch,
                worktree = excluded.worktree,
                session_id = excluded.session_id,
                verdict = excluded.verdict,
                merge_sha = excluded.merge_sha,
                policy_passed = excluded.policy_passed,
                cancel_requested = excluded.cancel_requested,
                updated = excluded.updated,
                payload = excluded.payload
            """,
            values,
        )


INTERRUPTED = ("queued", "running", "merging")


def reconcile_runs(conn: sqlite3.Connection, *, now: str | None = None) -> list[str]:
    """A run lives in a daemon thread. A restart leaves its row behind: mark it
    interrupted and release the task lock, or the project stays blocked."""
    stamp = now or now_iso()
    rows = conn.execute(
        "SELECT id, payload FROM runs WHERE state IN (?, ?, ?)", INTERRUPTED
    ).fetchall()
    ids: list[str] = []
    for row in rows:
        try:
            data = json.loads(row["payload"])
        except ValueError:
            continue
        data["state"] = "error"
        data["error"] = (
            "interrupted: the daemon restarted while this run was active. "
            "Retry the task."
        )
        data["finished_at"] = stamp
        data["updated"] = stamp
        save_run(conn, data)
        release_lock(conn, data.get("task", ""), data.get("id", ""))
        ids.append(data.get("id", ""))
    return ids


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_runs(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    project: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if project:
        clauses.append("project = ?")
        params.append(project)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT payload FROM runs {where} ORDER BY created DESC LIMIT ?", params
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def active_run_for_project(
    conn: sqlite3.Connection, project: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM runs WHERE project = ? "
        "AND state IN ('queued', 'running', 'merging') "
        "ORDER BY created DESC LIMIT 1",
        (project,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def recent_runs_for_task(
    conn: sqlite3.Connection, task: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM runs WHERE task = ? "
        "ORDER BY created DESC LIMIT ?",
        (task, limit),
    ).fetchall()
    return [json.loads(row["payload"]) for row in reversed(rows)]


# --------------------------------------------------------- work sessions
def get_work_session(
    conn: sqlite3.Connection, project: str, task: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT project, task, author_session, critic_session, updated "
        "FROM work_sessions WHERE project = ? AND task = ?",
        (project, task),
    ).fetchone()
    return dict(row) if row else None


def save_work_session(
    conn: sqlite3.Connection,
    project: str,
    task: str,
    *,
    author_session: str | None = None,
    critic_session: str | None = None,
) -> None:
    current = get_work_session(conn, project, task) or {}
    author = author_session or current.get("author_session")
    critic = critic_session or current.get("critic_session")
    with conn:
        conn.execute(
            """
            INSERT INTO work_sessions (
                project, task, author_session, critic_session, updated
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, task) DO UPDATE SET
                author_session = excluded.author_session,
                critic_session = excluded.critic_session,
                updated = excluded.updated
            """,
            (project, task, author, critic, now_iso()),
        )


def clear_work_session(
    conn: sqlite3.Connection, project: str, task: str
) -> None:
    with conn:
        conn.execute(
            "DELETE FROM work_sessions WHERE project = ? AND task = ?",
            (project, task),
        )


def request_cancel(conn: sqlite3.Connection, run_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,)
        )


def cancel_requested(conn: sqlite3.Connection, run_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return bool(row and row["cancel_requested"])


# ----------------------------------------------------------------- shapes
def save_shape(conn: sqlite3.Connection, shape: dict[str, Any]) -> None:
    payload = json.dumps(shape, ensure_ascii=False)
    values = (
        shape["id"],
        shape["project"],
        shape.get("state", "drafting"),
        shape.get("branch", ""),
        shape.get("worktree", ""),
        shape.get("run_dir", ""),
        shape.get("base_sha", ""),
        int(shape.get("attempt", 1)),
        shape.get("created"),
        shape.get("updated"),
        payload,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO shapes (
                id, project, state, branch, worktree, run_dir, base_sha,
                attempt, created, updated, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project = excluded.project,
                state = excluded.state,
                branch = excluded.branch,
                worktree = excluded.worktree,
                run_dir = excluded.run_dir,
                base_sha = excluded.base_sha,
                attempt = excluded.attempt,
                updated = excluded.updated,
                payload = excluded.payload
            """,
            values,
        )


def get_shape(conn: sqlite3.Connection, shape_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM shapes WHERE id = ?", (shape_id,)
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def claim_shape(
    conn: sqlite3.Connection,
    shape_id: str,
    *,
    from_states: tuple[str, ...],
    to_state: str,
) -> bool:
    """Atomically move a shape out of an idle state. Only one caller wins."""
    if not from_states:
        return False
    placeholders = ", ".join("?" for _ in from_states)
    with conn:
        cursor = conn.execute(
            f"UPDATE shapes SET state = ?, updated = ? "
            f"WHERE id = ? AND state IN ({placeholders})",
            (to_state, now_iso(), shape_id, *from_states),
        )
    return cursor.rowcount == 1


def latest_shape(
    conn: sqlite3.Connection, project: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM shapes WHERE project = ? "
        "ORDER BY created DESC LIMIT 1",
        (project,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def active_shape_for_project(
    conn: sqlite3.Connection, project: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM shapes WHERE project = ? "
        "AND state NOT IN ('approved', 'cancelled') "
        "ORDER BY created DESC LIMIT 1",
        (project,),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_shapes(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if project:
        rows = conn.execute(
            "SELECT payload FROM shapes WHERE project = ? "
            "ORDER BY created DESC LIMIT ?",
            (project, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload FROM shapes ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


# ---------------------------------------------------------------- events
def record_event(
    conn: sqlite3.Connection, kind: str, payload: dict[str, Any]
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
            (now_iso(), kind, json.dumps(payload, ensure_ascii=False)),
        )


# ------------------------------------------------------------ turn jobs
def create_job(
    conn: sqlite3.Connection,
    *,
    shape: str,
    project: str,
    kind: str,
    title: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    job_id = f"j_{secrets.token_hex(4)}"
    with conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM turn_jobs "
            "WHERE shape = ?",
            (shape,),
        ).fetchone()
        position = int(row["next"])
        conn.execute(
            "INSERT INTO turn_jobs (id, shape, project, kind, title, payload, "
            "status, position, created) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                job_id,
                shape,
                project,
                kind,
                title,
                json.dumps(payload, ensure_ascii=False),
                position,
                now_iso(),
            ),
        )
    return get_job(conn, job_id)


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except ValueError:
        data["payload"] = {}
    return data


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM turn_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_row(row) if row else None


def list_jobs(
    conn: sqlite3.Connection, shape: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM turn_jobs WHERE shape = ? ORDER BY position ASC LIMIT ?",
        (shape, limit),
    ).fetchall()
    return [_job_row(row) for row in rows]


def next_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM turn_jobs WHERE status = 'queued' "
        "AND shape NOT IN (SELECT shape FROM turn_jobs WHERE status = 'running') "
        "ORDER BY created ASC LIMIT 1"
    ).fetchone()
    return _job_row(row) if row else None


def claim_job(conn: sqlite3.Connection, job_id: str) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE turn_jobs SET status = 'running', started = ?, "
            "attempts = attempts + 1 WHERE id = ? AND status = 'queued'",
            (now_iso(), job_id),
        )
    return cursor.rowcount == 1


def requeue_job(conn: sqlite3.Connection, job_id: str, error: str | None) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE turn_jobs SET status = 'queued', started = NULL, error = ? "
            "WHERE id = ? AND status = 'running'",
            (error, job_id),
        )
    return cursor.rowcount == 1


def finish_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str = "done",
    reply: str | None = None,
    error: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    context: int = 0,
) -> None:
    with conn:
        conn.execute(
            "UPDATE turn_jobs SET status = ?, reply = ?, error = ?, "
            "tokens_in = ?, tokens_out = ?, cost = ?, context = ?, finished = ? "
            "WHERE id = ?",
            (
                status,
                reply,
                error,
                tokens_in,
                tokens_out,
                cost,
                context,
                now_iso(),
                job_id,
            ),
        )


def cancel_job(conn: sqlite3.Connection, job_id: str) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE turn_jobs SET status = 'cancelled', finished = ? "
            "WHERE id = ? AND status = 'queued'",
            (now_iso(), job_id),
        )
    return cursor.rowcount == 1


def requeue_running_jobs(conn: sqlite3.Connection) -> int:
    with conn:
        cursor = conn.execute(
            "UPDATE turn_jobs SET status = 'queued', started = NULL, "
            "error = 'requeued after restart' WHERE status = 'running'"
        )
    return cursor.rowcount
