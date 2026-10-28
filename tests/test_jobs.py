from __future__ import annotations

from datetime import UTC, datetime

from command_center import db
from command_center.config import Config
from command_center.jobs import JobRunner, job_title
from command_center.shape import ShapeRecord


def make_record(shape: str = "s_1", project: str = "p1") -> ShapeRecord:
    now = datetime.now(UTC)
    return ShapeRecord(
        id=shape,
        project=project,
        branch="shape-1",
        worktree="/tmp/wt",
        run_dir="/tmp/run",
        base_sha="0" * 40,
        state="questions",
        created=now,
        updated=now,
    )


class FakeShapeEngine:
    def __init__(self):
        self.calls: list[tuple] = []
        self.record = make_record()

    def status(self, project):
        return {"shape": self.record.model_dump(mode="json")}

    def answer(self, project, question_id, value, *, in_turn=False):
        assert in_turn is True
        self.calls.append(("answer", project, question_id, value))

    def revise(self, record, instruction=None):
        self.calls.append(("revise", record.id, instruction))
        return record.model_copy(
            update={
                "last_reply": "revised the spec",
                "last_usage": {
                    "tokens_in": 120,
                    "tokens_out": 30,
                    "cost": 0.01,
                    "context": 120,
                },
            }
        )

    def derive_c4(self, record):
        self.calls.append(("c4", record.id))
        return record.model_copy(
            update={
                "last_reply": "four levels",
                "last_usage": {"tokens_in": 70, "tokens_out": 20, "cost": 0.002, "context": 70},
            }
        )

    def derive_use_cases(self, record):
        self.calls.append(("workflows", record.id))
        return record.model_copy(
            update={
                "last_reply": "three flows",
                "last_usage": {
                    "tokens_in": 90,
                    "tokens_out": 20,
                    "cost": 0.003,
                    "context": 90,
                },
            }
        )

    def discuss(self, project, message, question_id=None):
        self.calls.append(("discuss", project, message, question_id))
        return {
            "reply": "answer about the question",
            "tokens_in": 40,
            "tokens_out": 8,
            "cost": 0.002,
            "context": 40,
        }


def test_job_titles() -> None:
    assert job_title("answers", {"answers": [{"id": "q1", "value": "a"}]}) == "answer q1"
    assert job_title(
        "answers",
        {"answers": [{"id": "q1", "value": "a"}, {"id": "q2", "value": "b"}]},
    ) == "answer q1 +1"
    assert job_title("discuss", {"question_id": "q3"}) == "discuss q3"
    assert job_title("revise", {"scope": "tree"}) == "revise tree"
    assert job_title("workflows", {}) == "use cases"
    assert job_title("c4", {}) == "C4 model"


def test_jobs_are_fifo_and_one_at_a_time(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)

    first = runner.enqueue(
        shape="s_1",
        project="p1",
        kind="answers",
        payload={"answers": [{"id": "q1", "value": "yes"}]},
    )
    second = runner.enqueue(
        shape="s_1",
        project="p1",
        kind="discuss",
        payload={"message": "why?", "question_id": "q2"},
    )
    assert first["position"] == 1
    assert second["position"] == 2

    assert runner._tick() is True
    assert runner._tick() is True
    assert runner._tick() is False

    assert engine.calls == [
        ("answer", "p1", "q1", "yes"),
        ("revise", "s_1", None),
        ("discuss", "p1", "why?", "q2"),
    ]
    jobs = {job["id"]: job for job in runner.list("s_1")}
    assert jobs[first["id"]]["status"] == "done"
    assert jobs[first["id"]]["reply"] == "revised the spec"
    assert jobs[first["id"]]["tokens_in"] == 120
    assert jobs[second["id"]]["reply"] == "answer about the question"
    assert jobs[second["id"]]["cost"] > 0


def test_next_job_never_picks_a_busy_shape(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)
    first = runner.enqueue(
        shape="s_1", project="p1", kind="discuss", payload={"message": "a"}
    )
    second = runner.enqueue(
        shape="s_1", project="p1", kind="discuss", payload={"message": "b"}
    )
    other = runner.enqueue(
        shape="s_2", project="p2", kind="discuss", payload={"message": "c"}
    )

    conn = db.connect(config.state_dir)
    try:
        job = db.next_job(conn)
        assert job is not None and job["id"] == first["id"]
        assert db.claim_job(conn, first["id"])
        # s_1 already has a running job, so its queued job must yield to s_2
        following = db.next_job(conn)
        assert following is not None
        assert following["id"] == other["id"]
        db.finish_job(conn, first["id"], reply="done")
        remaining = db.next_job(conn)
        assert remaining is not None
        assert remaining["id"] == second["id"]
    finally:
        conn.close()


def test_running_jobs_are_requeued_after_a_restart(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)
    job = runner.enqueue(
        shape="s_1", project="p1", kind="discuss", payload={"message": "x"}
    )
    conn = db.connect(config.state_dir)
    try:
        assert db.claim_job(conn, job["id"])
        assert db.requeue_running_jobs(conn) == 1
        again = db.get_job(conn, job["id"])
        assert again is not None
        assert again["status"] == "queued"
    finally:
        conn.close()


def test_cancel_only_removes_queued_jobs(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)
    job = runner.enqueue(
        shape="s_1", project="p1", kind="discuss", payload={"message": "x"}
    )
    assert runner.cancel(job["id"]) is True
    assert runner.cancel(job["id"]) is False
    assert runner._tick() is False


class FlakyEngine(FakeShapeEngine):
    def __init__(self, message: str, fail_times: int = 1):
        super().__init__()
        self.message = message
        self.remaining = fail_times

    def revise(self, record, instruction=None):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError(self.message)
        return super().revise(record, instruction)


def test_transport_errors_are_retried_once(config: Config) -> None:
    engine = FlakyEngine("Transport: ECONNRESET: socket closed")
    runner = JobRunner(config, engine, autostart=False)
    runner.enqueue(
        shape="s_1",
        project="p1",
        kind="revise",
        payload={"instruction": "split the task"},
    )

    assert runner._tick() is True  # fails, requeues
    stored = runner.list("s_1")[0]
    assert stored["status"] == "queued"
    assert "ECONNRESET" in (stored["error"] or "")

    assert runner._tick() is True  # succeeds
    stored = runner.list("s_1")[0]
    assert stored["status"] == "done"
    assert stored["attempts"] == 2
    assert stored["reply"] == "revised the spec"


def test_non_transport_errors_are_not_retried(config: Config) -> None:
    engine = FlakyEngine("permission denied writing docs/spec", fail_times=5)
    runner = JobRunner(config, engine, autostart=False)
    runner.enqueue(
        shape="s_1", project="p1", kind="revise", payload={"instruction": "x"}
    )

    assert runner._tick() is True
    stored = runner.list("s_1")[0]
    assert stored["status"] == "failed"
    assert stored["attempts"] == 1
    assert "permission denied" in (stored["error"] or "")


def test_workflows_job_runs_one_turn(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)
    job = runner.enqueue(
        shape="s_1", project="p1", kind="workflows", payload={}
    )
    assert job["title"] == "use cases"
    assert runner._tick() is True
    assert engine.calls == [("workflows", "s_1")]
    stored = runner.list("s_1")[0]
    assert stored["status"] == "done"
    assert stored["reply"] == "three flows"


def test_c4_job_runs_one_turn(config: Config) -> None:
    engine = FakeShapeEngine()
    runner = JobRunner(config, engine, autostart=False)
    job = runner.enqueue(shape="s_1", project="p1", kind="c4", payload={})
    assert job["title"] == "C4 model"
    assert runner._tick() is True
    assert engine.calls == [("c4", "s_1")]
    assert runner.list("s_1")[0]["status"] == "done"
