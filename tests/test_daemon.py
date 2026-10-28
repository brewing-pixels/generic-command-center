from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import (
    FakeAgent,
    FakeInquisitor,
    FakeTaskStore,
    commit,
    make_agenda,
    make_project,
    spec_state,
    work_item,
)

from command_center import db, gitutil, spec
from command_center.config import Config
from command_center.daemon import create_app
from command_center.events import EventBus
from command_center.runner import RunEngine
from command_center.shape import ShapeEngine
from command_center.taskstore import TaskStoreTask
from command_center.token import ensure_token


def build(repo: Path, config: Config, agenda=None, shape_engine=None):
    (repo / "command-center.yaml").write_text(
        'version: 1\ntasks:\n  t1:\n    gate:\n      - ["true"]\n    scope:\n      - "src/**"\n',
        encoding="utf-8",
    )
    commit(repo, "plan")
    digest = spec.compute_digest(repo, ["SPEC.md"], "HEAD")
    project = make_project(
        path=str(repo),
        tasks=[TaskStoreTask(id="t1", title="One")],
        spec_state_value=spec_state(["SPEC.md"], digest),
    )
    taskstore = FakeTaskStore([project], agenda)
    engine = RunEngine(config, taskstore, bus=EventBus(), agent=FakeAgent())
    app = create_app(config, taskstore=taskstore, engine=engine, shape_engine=shape_engine)
    return app, taskstore


def test_auth_and_run(repo: Path, config: Config) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/runs").status_code == 401

    focus = client.get("/api/focus", headers=headers)
    assert focus.status_code == 200, focus.text
    assert focus.json()["entries"] == []

    started = client.post("/api/runs", headers=headers, json={"task": "t1"})
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    deadline = time.time() + 20
    run = started.json()
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}", headers=headers).json()
        if run["state"] in ("merged", "blocked", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert run["state"] == "merged", run
    assert run["merge_sha"]
    assert gitutil.head_commit(repo, "cc/integration") == run["merge_sha"]

    run_dir = Path(run["run_dir"])
    (run_dir / "agent.jsonl").write_text("one\ntwo\n", encoding="utf-8")
    logs = client.get(f"/api/runs/{run_id}/logs?stream=agent&offset=0", headers=headers)
    assert logs.status_code == 200, logs.text
    assert logs.json()["text"] == "one\ntwo\n"
    assert logs.json()["eof"] is True
    tail = client.get(f"/api/runs/{run_id}/logs?stream=agent&offset=4", headers=headers)
    assert tail.json()["text"] == "two\n"
    assert tail.json()["offset"] == len("one\ntwo\n")
    missing = client.get(f"/api/runs/{run_id}/logs?stream=nope", headers=headers)
    assert missing.status_code == 404


def test_web_shell_and_projects(repo: Path, config: Config) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "COMMAND" in root.text and "CENTER" in root.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/systemcraft.css").status_code == 200

    assert client.get("/api/projects").status_code == 401
    projects = client.get("/api/projects", headers=headers)
    assert projects.status_code == 200
    assert projects.json()["projects"][0]["id"] == "p1"
    assert client.get("/api/projects?token=wrong").status_code == 401
    assert client.get(f"/api/projects?token={token}").status_code == 200

    settings = client.get("/api/settings", headers=headers)
    assert settings.status_code == 200
    body = settings.json()
    assert body["roles"] == {"inquisitor": "hosted", "coder": "hosted", "reviewer": "hosted"}
    assert body["paths"]["token_exists"] is True
    assert "PROVIDER_API_KEY" not in json.dumps(body)
    assert token not in json.dumps(body)


def test_artifacts_and_agenda_proxy(repo: Path, config: Config) -> None:
    agenda = make_agenda(
        day=date.today(),
        focus=["p1"],
        items=[work_item()],
    )
    agenda = agenda.model_copy(update={"status": "locked", "rev": 1})
    app, taskstore = build(repo, config, agenda=agenda)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    got = client.get(f"/api/agendas/{date.today()}", headers=headers)
    assert got.status_code == 200
    assert got.json()["items"][0]["task"] == "t1"

    suggested = client.post(
        f"/api/agendas/{date.today()}/suggest",
        headers=headers,
        json={"replace": True},
    )
    assert suggested.status_code == 200, suggested.text

    stale_lock = client.post(
        f"/api/agendas/{date.today()}/lock",
        headers=headers,
        json={"if_rev": 999},
    )
    assert stale_lock.status_code == 409, stale_lock.text
    locked = client.post(
        f"/api/agendas/{date.today()}/lock",
        headers=headers,
        json={"if_rev": 1},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["already_locked"] is True
    no_body = client.post(f"/api/agendas/{date.today()}/lock", headers=headers)
    assert no_body.status_code == 200, no_body.text

    started = client.post("/api/runs", headers=headers, json={"task": "t1"})
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}", headers=headers).json()
        if run["state"] in ("merged", "blocked", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert run["state"] == "merged", run

    for kind in ("diff", "gate", "policy", "review", "prompt", "run"):
        artifact = client.get(f"/api/runs/{run_id}/artifacts/{kind}", headers=headers)
        assert artifact.status_code == 200, (kind, artifact.text)
    review = client.get(f"/api/runs/{run_id}/artifacts/review", headers=headers)
    assert review.json()["verdict"] == "approve"
    missing = client.get(f"/api/runs/{run_id}/artifacts/nope", headers=headers)
    assert missing.status_code == 404

    agenda = make_agenda(
        day=date.today(),
        focus=["p1"],
        items=[work_item()],
    ).model_copy(update={"status": "draft", "rev": 0})
    taskstore.agenda = agenda
    put = client.put(
        f"/api/agendas/{date.today()}",
        headers=headers,
        json={
            "focus": ["p1"],
            "capacity": {"work_items": 3, "chores": 3, "operator_min": 90},
            "items": [
                {
                    "kind": "work",
                    "project": "p1",
                    "objective": "o_general",
                    "task": "t1",
                    "reason": "proxy",
                    "order": 0,
                }
            ],
            "if_rev": 0,
        },
    )
    assert put.status_code == 200, put.text
    assert put.json()["items"][0]["reason"] == "proxy"


def test_quick_add_creates_task_and_manual_item(repo: Path, config: Config) -> None:
    agenda = make_agenda(day=date.today(), focus=["p1"], items=[])
    app, taskstore = build(repo, config, agenda=agenda)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    response = client.post(
        f"/api/agendas/{date.today()}/quick-add",
        headers=headers,
        json={
            "project": "p1",
            "title": "Call the dentist",
            "due_time": "09:30",
            "reason": "quick add",
            "if_rev": 0,
        },
    )
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["title"] == "Call the dentist"
    assert response.json()["agenda"]["items"][0]["kind"] == "manual"
    assert response.json()["agenda"]["items"][0]["task"] == task["id"]
    assert any(
        item.id == task["id"]
        for objective in taskstore.projects["p1"].objectives
        for item in objective.tasks
    )

    stale = client.post(
        f"/api/agendas/{date.today()}/quick-add",
        headers=headers,
        json={"project": "p1", "title": "Stale", "if_rev": 0},
    )
    assert stale.status_code == 409, stale.text

    taskstore.agenda = taskstore.agenda.model_copy(update={"status": "locked"})
    locked = client.post(
        f"/api/agendas/{date.today()}/quick-add",
        headers=headers,
        json={"project": "p1", "title": "Locked"},
    )
    assert locked.status_code == 409, locked.text


TASKS_DRAFT = """version: 1
objectives:
  - slug: build
    title: Build the thing
    acceptance:
      - the thing exists
    gate:
      - ["true"]
    tasks:
      - slug: hello
        title: Create hello
        acceptance:
          - hello exists
        gate:
          - ["true"]
        depends_on: []
        scope:
          - "hello.txt"
"""


def test_create_project_objective_and_task(
    repo: Path, config: Config
) -> None:
    agenda = make_agenda(items=[])
    app, _ = build(repo, config, agenda=agenda)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    created = client.post(
        "/api/projects", headers=headers, json={"name": "Fresh", "path": str(repo)}
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project"]["id"]
    assert created.json()["project"]["name"] == "Fresh"

    objective = client.post(
        f"/api/projects/{project_id}/objectives",
        headers=headers,
        json={"title": "Build it"},
    )
    assert objective.status_code == 201, objective.text
    objective_id = objective.json()["objective"]["id"]

    task = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "project": project_id,
            "objective": objective_id,
            "title": "Do the thing",
        },
    )
    assert task.status_code == 201, task.text
    assert task.json()["task"]["title"] == "Do the thing"
    assert "agenda" not in task.json()

    with_agenda = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "project": project_id,
            "objective": objective_id,
            "title": "Today thing",
            "add_to_today": True,
            "if_rev": agenda.rev,
        },
    )
    assert with_agenda.status_code == 201, with_agenda.text
    assert with_agenda.json()["agenda"]["items"][-1]["task"] == (
        with_agenda.json()["task"]["id"]
    )


def test_orphaned_runs_are_marked_interrupted(config: Config) -> None:
    conn = db.connect(config.state_dir)
    try:
        now = "2026-10-27T10:00:00Z"
        db.save_run(
            conn,
            {
                "id": "r_orphan",
                "kind": "build",
                "project": "p1",
                "objective": "o_general",
                "task": "t1",
                "state": "running",
                "attempt": 2,
                "created": now,
                "updated": now,
            },
        )
        db.acquire_lock(conn, "t1", "r_orphan")
        assert db.lock_exists(conn, "t1")

        assert db.reconcile_runs(conn, now=now) == ["r_orphan"]

        run = db.get_run(conn, "r_orphan")
        assert run is not None
        assert run["state"] == "error"
        assert "interrupted" in run["error"]
        assert run["finished_at"] == now
        assert db.lock_exists(conn, "t1") is False
    finally:
        conn.close()


def test_project_build_walks_the_locked_agenda(
    repo: Path, config: Config
) -> None:
    agenda = make_agenda(items=[work_item(task="t1")]).model_copy(
        update={"status": "locked"}
    )
    app, _ = build(repo, config, agenda=agenda)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    started = client.post("/api/projects/p1/build", headers=headers)
    assert started.status_code == 202, started.text
    assert started.json()["active"] is True

    deadline = time.time() + 20
    status = started.json()
    while time.time() < deadline:
        status = client.get("/api/projects/p1/build", headers=headers).json()
        if not status["active"]:
            break
        time.sleep(0.05)
    assert status["active"] is False, status
    assert status["merged"] == 1, status
    assert status["note"] == "nothing left to build", status

    runs = client.get("/api/runs", headers=headers).json()
    assert [run["state"] for run in runs] == ["merged"]


def test_project_build_needs_a_locked_agenda(
    repo: Path, config: Config
) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    refused = client.post("/api/projects/p1/build", headers=headers)
    assert refused.status_code == 409, refused.text
    assert "lock" in refused.text


def test_add_a_batch_of_tasks_to_the_agenda(
    repo: Path, config: Config
) -> None:
    agenda = make_agenda(items=[])
    taskstore = FakeTaskStore(
        [
            make_project(
                path=str(repo),
                tasks=[
                    TaskStoreTask(id="t1", title="One"),
                    TaskStoreTask(id="t2", title="Two"),
                ],
            )
        ],
        agenda,
    )
    engine = RunEngine(config, taskstore, bus=EventBus(), agent=FakeAgent())
    app = create_app(config, taskstore=taskstore, engine=engine)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    body = {
        "tasks": [
            {"project": "p1", "objective": "o_general", "task": "t1"},
            {"project": "p1", "objective": "o_general", "task": "t2"},
            {"project": "p1", "objective": "o_general", "task": "gone"},
        ],
        "if_rev": agenda.rev,
    }
    first = client.post(
        f"/api/agendas/{date.today()}/tasks", headers=headers, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json()["added"] == 2
    assert first.json()["skipped"] == 1
    assert [item["task"] for item in first.json()["agenda"]["items"]] == ["t1", "t2"]

    body["if_rev"] = first.json()["agenda"]["rev"]
    again = client.post(
        f"/api/agendas/{date.today()}/tasks", headers=headers, json=body
    )
    assert again.status_code == 200, again.text
    assert again.json()["added"] == 0
    assert again.json()["skipped"] == 3


def test_create_task_without_an_agenda_is_refused(
    repo: Path, config: Config
) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    refused = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "project": "p1",
            "objective": "o_general",
            "title": "Orphan",
            "add_to_today": True,
        },
    )
    assert refused.status_code == 409, refused.text


def test_project_spec_reads_the_integration_ref(
    repo: Path, config: Config
) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    base = gitutil.current_branch(repo)
    gitutil._run(repo, "checkout", "-q", "-b", "cc/integration")
    docs = repo / "docs" / "spec"
    docs.mkdir(parents=True)
    (docs / "SPEC.md").write_text("# Integration spec\n", encoding="utf-8")
    (docs / "USE_CASES.yaml").write_text(
        "version: 1\nuse_cases:\n  - id: uc1\n    title: Say hi\n"
        "    steps:\n      - label: Greet\n",
        encoding="utf-8",
    )
    (repo / "SPEC.md").write_text("# Legacy spec\n", encoding="utf-8")
    commit(repo, "canonical spec")
    gitutil._run(repo, "checkout", "-q", base)

    payload = client.get("/api/projects/p1/spec", headers=headers)
    assert payload.status_code == 200, payload.text
    data = payload.json()
    assert data["ref"] == "cc/integration"
    assert [item["path"] for item in data["files"]] == ["docs/spec/SPEC.md"]
    assert "Integration spec" in data["files"][0]["text"]
    assert data["use_cases"]["use_cases"][0]["id"] == "uc1"
    assert data["c4"] is None
    assert data["errors"] == []


def test_shape_api_flow(repo: Path, config: Config) -> None:
    project = make_project(path=str(repo), spec_state_value=None)
    taskstore = FakeTaskStore([project])
    inquisitor = FakeInquisitor(
        [
            {
                "docs/spec/SPEC.md": "# Shape\n\nBuild one thing.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": (
                    "version: 1\nquestions:\n  - id: q1\n"
                    "    question: Name?\n    default: hello\n    answer: null\n"
                ),
            },
            {
                "docs/spec/SPEC.md": "# Shape\n\nBuild one thing named hello.\n",
                "docs/spec/TASKS.yaml": TASKS_DRAFT,
                "docs/spec/QUESTIONS.yaml": "version: 1\nquestions: []\n",
            },
        ]
    )
    shape_engine = ShapeEngine(config, taskstore, agent=inquisitor)
    app, _ = build(repo, config, shape_engine=shape_engine)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    started = client.post("/api/shapes", headers=headers, json={"project": "p1"})
    assert started.status_code == 201, started.text

    deadline = time.time() + 10
    state = None
    while time.time() < deadline:
        status = client.get("/api/shapes/p1", headers=headers)
        assert status.status_code == 200, status.text
        state = status.json()["shape"]["state"]
        if state in {"questions", "ready", "error", "check_failed"}:
            break
        time.sleep(0.05)
    assert state == "questions", state

    queued = client.post(
        "/api/shapes/p1/jobs",
        headers=headers,
        json={"kind": "answers", "answers": {"q1": "hello"}},
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["title"] == "answer q1"

    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get("/api/shapes/p1", headers=headers).json()
        state = status["shape"]["state"]
        if state in {"ready", "check_failed", "error"}:
            break
        time.sleep(0.05)
    assert state == "ready", status
    jobs = client.get("/api/shapes/p1/jobs", headers=headers).json()["jobs"]
    assert [job["kind"] for job in jobs] == ["draft", "answers"], jobs
    assert all(job["status"] == "done" for job in jobs), jobs

    docs = client.get("/api/shapes/p1/spec", headers=headers)
    assert docs.status_code == 200, docs.text
    assert [doc["name"] for doc in docs.json()["files"]] == ["SPEC.md"]

    saved = client.put(
        "/api/shapes/p1/use-cases",
        headers=headers,
        json={
            "version": 1,
            "use_cases": [
                {
                    "id": "uc1",
                    "title": "Say hello",
                    "actor": "Operator",
                    "steps": [{"label": "Create hello", "task": "hello"}],
                }
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["check"]["ok"] is True

    stale = client.put(
        "/api/shapes/p1/use-cases",
        headers=headers,
        json={
            "version": 1,
            "use_cases": [
                {
                    "id": "uc1",
                    "title": "Say hello",
                    "steps": [{"label": "Do it", "task": "missing"}],
                }
            ],
        },
    )
    assert stale.status_code == 409, stale.text
    assert "unknown task" in stale.text

    checked = client.post("/api/shapes/p1/check", headers=headers)
    assert checked.status_code == 200, checked.text
    assert checked.json()["check"]["ok"] is True

    materialized = client.post("/api/shapes/p1/materialize", headers=headers)
    assert materialized.status_code == 200, materialized.text
    assert materialized.json()["state"] == "materialized"

    approved = client.post("/api/shapes/p1/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    latest = client.get("/api/shapes/p1", headers=headers).json()
    assert latest["shape"]["state"] == "approved"
    assert taskstore.get_project("p1").spec_state.phase == "approved"


def test_pin_login_issues_a_session_token(repo: Path, config: Config) -> None:
    cfg = config.model_copy(
        update={
            "daemon": config.daemon.model_copy(update={"pin": "2468"}),
        }
    )
    app, _ = build(repo, cfg)
    token = ensure_token(cfg.token_path)
    client = TestClient(app)

    assert client.post("/api/auth/pin", json={"pin": "0000"}).status_code == 401
    assert client.post("/api/auth/pin", json={"pin": "2468"}).status_code == 200
    session = client.post("/api/auth/pin", json={"pin": "2468"}).json()["token"]
    assert session != token
    assert (
        client.get("/api/projects", headers={"Authorization": f"Bearer {session}"}).status_code
        == 200
    )
    assert client.get("/api/projects", headers={"Authorization": "Bearer 2468"}).status_code == 401
    assert (
        client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


def test_shape_jobs_endpoint_guards(repo: Path, config: Config) -> None:
    app, _ = build(repo, config)
    token = ensure_token(config.token_path)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    missing = client.post(
        "/api/shapes/p1/jobs", headers=headers, json={"kind": "discuss", "message": "hi"}
    )
    assert missing.status_code == 404, missing.text

    unknown = client.post(
        "/api/shapes/p1/jobs", headers=headers, json={"kind": "curse"}
    )
    assert unknown.status_code == 422, unknown.text
