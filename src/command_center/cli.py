from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__, db
from .client import DaemonClient, DaemonError
from .config import REPO_ROOT, Config, load_config
from .eligibility import EligibilityService, Reason, TaskCheck
from .errors import CommandCenterError, TaskStoreError
from .probe import ModelProbe
from .shape import check_shape
from .spec import approve as approve_spec
from .taskstore import TaskStoreAgenda, TaskStoreClient

TERMINAL = {"merged", "blocked", "error", "cancelled"}


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from exc


def _day(value: date | None) -> date:
    return value or date.today()


def _reason(reason: Reason) -> dict[str, str]:
    return {"code": reason.code, "detail": reason.detail}


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(str(row[index])) for row in [headers, *rows]) for index in range(len(headers))
    ]
    lines = ["  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc", description="Command Center")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="project-level queue summary")
    status.add_argument("--json", action="store_true")

    tasks = sub.add_parser("tasks", help="task eligibility with reasons")
    tasks.add_argument("--project")
    tasks.add_argument("--all", action="store_true", help="include closed tasks")
    tasks.add_argument("--json", action="store_true")

    models = sub.add_parser("models", help="model availability")
    models.add_argument("--json", action="store_true")

    focus = sub.add_parser("focus", help="focus projects")
    focus.add_argument("--json", action="store_true")
    focus_sub = focus.add_subparsers(dest="focus_command")
    focus_sub.add_parser("list", help="show the focus list")
    focus_set = focus_sub.add_parser("set", help="replace the focus list")
    focus_set.add_argument("ids", nargs="*")
    focus_sub.add_parser("clear", help="clear the focus list")

    agenda = sub.add_parser("agenda", help="daily agenda")
    agenda_sub = agenda.add_subparsers(dest="agenda_command", required=True)
    agenda_show = agenda_sub.add_parser("show", help="show an agenda")
    agenda_show.add_argument("date", nargs="?", type=_date)
    agenda_show.add_argument("--json", action="store_true")
    agenda_suggest = agenda_sub.add_parser("suggest", help="generate a draft")
    agenda_suggest.add_argument("date", nargs="?", type=_date)
    agenda_suggest.add_argument("--replace", action="store_true")
    agenda_lock = agenda_sub.add_parser("lock", help="validate and lock")
    agenda_lock.add_argument("date", nargs="?", type=_date)
    agenda_unlock = agenda_sub.add_parser("unlock", help="unlock with a reason")
    agenda_unlock.add_argument("date", nargs="?", type=_date)
    agenda_unlock.add_argument("--reason", required=True)

    spec = sub.add_parser("spec", help="spec approval")
    spec_sub = spec.add_subparsers(dest="spec_command", required=True)
    spec_approve = spec_sub.add_parser("approve", help="approve a committed spec")
    spec_approve.add_argument("project")
    spec_approve.add_argument(
        "--docs",
        help="comma-separated repo-relative docs; defaults to the project spec",
    )
    spec_check = spec_sub.add_parser("check", help="check a shape for pristine criteria")
    spec_check.add_argument("project")
    spec_check.add_argument("--json", action="store_true")

    shape = sub.add_parser("shape", help="inquisitor and decomposition")
    shape_sub = shape.add_subparsers(dest="shape_command", required=True)
    shape_start = shape_sub.add_parser(
        "start", help="prepare a shape worktree and run the inquisitor"
    )
    shape_start.add_argument("project")
    shape_start.add_argument("--rewrite", action="store_true")
    shape_start.add_argument(
        "--objective", help="seed the shape with this TaskStore objective id"
    )
    shape_start.add_argument(
        "--task", help="seed the shape with this TaskStore task id"
    )
    shape_start.add_argument("--model", help="inquisitor model id for this shape")
    shape_model = shape_sub.add_parser(
        "model", help="switch the inquisitor model of the active shape"
    )
    shape_model.add_argument("project")
    shape_model.add_argument("model")
    shape_status = shape_sub.add_parser("status", help="show the active or latest shape")
    shape_status.add_argument("project")
    shape_status.add_argument("--json", action="store_true")
    shape_answer = shape_sub.add_parser("answer", help="queue one answer")
    shape_answer.add_argument("project")
    shape_answer.add_argument("question_id")
    shape_answer.add_argument("answer")
    shape_answer.add_argument(
        "--no-wait", action="store_true", help="return once the turn is queued"
    )
    shape_revise = shape_sub.add_parser("revise", help="queue a revision pass")
    shape_revise.add_argument("project")
    shape_revise.add_argument(
        "--instruction", help="free-form direction for this pass"
    )
    shape_revise.add_argument(
        "--no-wait", action="store_true", help="return once the turn is queued"
    )
    shape_jobs = shape_sub.add_parser("jobs", help="list this shape's turn queue")
    shape_jobs.add_argument("project")
    shape_jobs.add_argument("--json", action="store_true")
    shape_check = shape_sub.add_parser("check", help="run the pristine check")
    shape_check.add_argument("project")
    shape_check.add_argument("--json", action="store_true")
    shape_materialize = shape_sub.add_parser(
        "materialize", help="create TaskStore tasks and the gate file"
    )
    shape_materialize.add_argument("project")
    shape_approve = shape_sub.add_parser("approve", help="merge the shape and approve its spec")
    shape_approve.add_argument("project")
    shape_cancel = shape_sub.add_parser("cancel", help="discard the shape worktree")
    shape_cancel.add_argument("project")
    shape_diff = shape_sub.add_parser("diff", help="show the shape batch diff")
    shape_diff.add_argument("project")
    shape_diff.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="start a task run")
    run.add_argument("task")
    run.add_argument("--model")
    run.add_argument("--reviewer")
    run.add_argument("--json", action="store_true")

    runs = sub.add_parser("runs", help="list runs or show one")
    runs.add_argument("run_id", nargs="?")
    runs.add_argument("--project")
    runs.add_argument("--state")
    runs.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="cancel a run")
    cancel.add_argument("run_id")

    agents = sub.add_parser("agents", help="agent files")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    install = agents_sub.add_parser("install", help="install agent files")
    install.add_argument("--dest")

    sub.add_parser("version", help="print the version")
    return parser


def _print_task(check: TaskCheck) -> None:
    reasons = ", ".join(reason.code for reason in check.result.reasons) or "ready"
    warnings = ", ".join(reason.code for reason in check.result.warnings)
    print(
        f"{check.project.id:<20} {check.task.id:<10} {check.task.status:<7} "
        f"{reasons}{'  warn: ' + warnings if warnings else ''}"
    )


def _cmd_status(checks: list[TaskCheck], as_json: bool) -> int:
    summary: dict[str, dict[str, Any]] = {}
    for check in checks:
        if check.task.status not in ("open", "doing"):
            continue
        entry = summary.setdefault(
            check.project.id,
            {
                "id": check.project.id,
                "name": check.project.name,
                "open": 0,
                "ready": 0,
                "blocked": 0,
                "reasons": {},
            },
        )
        entry["open"] += 1
        if check.result.ready:
            entry["ready"] += 1
        else:
            entry["blocked"] += 1
            for reason in check.result.reasons:
                entry["reasons"][reason.code] = entry["reasons"].get(reason.code, 0) + 1
    rows = sorted(summary.values(), key=lambda item: item["name"].lower())
    if as_json:
        print(json.dumps({"projects": rows}, indent=2))
        return 0
    table = [
        [
            row["id"],
            row["name"],
            str(row["open"]),
            str(row["ready"]),
            str(row["blocked"]),
            ", ".join(f"{code} ({count})" for code, count in sorted(row["reasons"].items())) or "-",
        ]
        for row in rows
    ]
    print(_table(["ID", "PROJECT", "OPEN", "READY", "BLOCKED", "TOP REASONS"], table))
    return 0


def _cmd_tasks(checks: list[TaskCheck], project: str | None, all_tasks: bool, as_json: bool) -> int:
    selected = [
        check
        for check in checks
        if (project is None or check.project.id == project)
        and (all_tasks or check.task.status in ("open", "doing"))
    ]
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "project": check.project.id,
                        "project_name": check.project.name,
                        "objective": check.objective.id,
                        "task": check.task.id,
                        "title": check.task.title,
                        "status": check.task.status,
                        "ready": check.result.ready,
                        "reasons": [_reason(reason) for reason in check.result.reasons],
                        "warnings": [_reason(reason) for reason in check.result.warnings],
                    }
                    for check in selected
                ],
                indent=2,
            )
        )
        return 0
    if not selected:
        print("no tasks")
        return 0
    for check in selected:
        _print_task(check)
    return 0


def _cmd_focus(daemon: DaemonClient, args: argparse.Namespace) -> int:
    if args.focus_command in ("set", "clear"):
        ids = args.ids if args.focus_command == "set" else []
        state = daemon.set_focus(ids)
    else:
        state = daemon.focus()
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2))
    else:
        entries = state.get("entries", [])
        if not entries:
            print("focus is empty")
        else:
            names = {project["id"]: project["name"] for project in state.get("projects", [])}
            rows = [
                [str(entry["position"]), entry["project"], names.get(entry["project"], "?")]
                for entry in entries
            ]
            print(_table(["POS", "PROJECT", "NAME"], rows))
            if state.get("stale"):
                print("focus set is older than 7 days")
        for error in state.get("tag_errors", []):
            print(f"warning: tag write failed: {error}", file=sys.stderr)
    return 0


def _cmd_models(config: Config, as_json: bool) -> int:
    statuses = ModelProbe(config).check_all()
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "id": status.id,
                        "label": status.label,
                        "model": status.model,
                        "up": status.up,
                        "startable": status.startable,
                        "reason": status.reason,
                    }
                    for status in statuses
                ],
                indent=2,
            )
        )
        return 0
    rows = [
        [
            status.id,
            status.label,
            status.model,
            "up" if status.up else "down",
            "startable" if status.startable else "-",
            status.reason,
        ]
        for status in statuses
    ]
    print(_table(["ID", "LABEL", "MODEL", "STATUS", "START", "REASON"], rows))
    return 0


def _print_agenda(agenda: TaskStoreAgenda) -> None:
    print(
        f"{agenda.date.isoformat()}  {agenda.status}  rev {agenda.rev}  "
        f"focus {','.join(agenda.focus) or '-'}"
    )
    if not agenda.items:
        print("  (no items)")
        return
    for item in agenda.items:
        print(
            f"  {item.id}  {item.kind:<6} {item.status:<8} "
            f"{item.project}/{item.task}  {item.reason}"
        )


def _cmd_agenda(taskstore: TaskStoreClient, daemon: DaemonClient, args: argparse.Namespace) -> int:
    day = _day(args.date)
    if args.agenda_command == "show":
        agenda = taskstore.get_agenda(day)
        if agenda is None:
            print(f"no agenda for {day.isoformat()}")
            return 0
        if args.json:
            print(json.dumps(agenda.model_dump(mode="json"), indent=2))
        else:
            _print_agenda(agenda)
        return 0
    if args.agenda_command == "suggest":
        agenda = taskstore.suggest_agenda(day, replace=args.replace)
        _print_agenda(agenda)
        return 0
    if args.agenda_command == "lock":
        result = daemon.lock_agenda(day)
        agenda = TaskStoreAgenda.model_validate(result["agenda"])
        state = "already locked" if result.get("already_locked") else "locked"
        print(f"{state}: {day.isoformat()} rev {agenda.rev}")
        for warning in result.get("warnings", []):
            print(
                f"warning: {warning['code']}: {warning['detail']}",
                file=sys.stderr,
            )
        return 0
    if args.agenda_command == "unlock":
        agenda = TaskStoreAgenda.model_validate(daemon.unlock_agenda(day, args.reason))
        print(f"unlocked: {day.isoformat()} rev {agenda.rev}")
        return 0
    raise CommandCenterError(f"unknown agenda command {args.agenda_command}")


def _cmd_run(daemon: DaemonClient, args: argparse.Namespace) -> int:
    record = daemon.start_run(args.task, args.model, args.reviewer)
    print(f"started {record['id']}")
    last_state = None
    while True:
        record = daemon.run(record["id"])
        state = record.get("state")
        if state != last_state:
            print(f"{state} (attempt {record.get('attempt', 1)})")
            last_state = state
        if state in TERMINAL:
            break
        time.sleep(1)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        for note in record.get("notes", []):
            print(f"  {note}")
        if record.get("merge_sha"):
            print(f"  merge {record['merge_sha']}")
        if record.get("error"):
            print(f"  error: {record['error']}")
    return 0 if record.get("state") == "merged" else 1


def _cmd_runs(daemon: DaemonClient, args: argparse.Namespace) -> int:
    if args.run_id:
        record = daemon.run(args.run_id)
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(
                f"{record['id']}  {record['project']}  {record['task']}  "
                f"{record['state']}  attempt {record.get('attempt', 1)}"
            )
            for note in record.get("notes", []):
                print(f"  {note}")
            if record.get("reviewer_verdict"):
                print(
                    f"  reviewer {record.get('reviewer_model') or '?'} → "
                    f"{record['reviewer_verdict']} ({record.get('reviewer_path')})"
                )
            if record.get("merge_sha"):
                print(f"  merge {record['merge_sha']}")
            if record.get("error"):
                print(f"  error: {record['error']}")
        return 0
    runs = daemon.runs(project=args.project, state=args.state)
    if args.json:
        print(json.dumps(runs, indent=2))
        return 0
    rows = [
        [
            run["id"],
            run["project"],
            run["task"],
            run["state"],
            str(run.get("attempt", 1)),
            run.get("merge_sha") or "-",
        ]
        for run in runs
    ]
    print(_table(["RUN", "PROJECT", "TASK", "STATE", "TRY", "MERGE"], rows))
    return 0


def _cmd_cancel(daemon: DaemonClient, args: argparse.Namespace) -> int:
    daemon.cancel_run(args.run_id)
    print(f"cancel requested: {args.run_id}")
    return 0


def _cmd_agents(config: Config, args: argparse.Namespace) -> int:
    source = REPO_ROOT / "agents"
    if not source.is_dir():
        raise CommandCenterError(f"agent source not found: {source}")
    destination = Path(args.dest or config.opencode.agents_dir).expanduser()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.glob("*.md")):
            shutil.copy2(path, destination / path.name)
            print(f"installed {destination / path.name}")
    except PermissionError as exc:
        raise CommandCenterError(
            f"cannot write {destination}; rerun with sudo or set --dest"
        ) from exc
    return 0


def _cmd_spec(taskstore: TaskStoreClient, conn: Any, args: argparse.Namespace) -> int:
    if args.spec_command == "check":
        return _cmd_spec_check(taskstore, conn, args)
    docs = None
    if args.docs:
        docs = [doc.strip() for doc in args.docs.split(",") if doc.strip()]
    result = approve_spec(taskstore, args.project, docs)
    print(f"approved {result.project_id}")
    print(f"  commit {result.commit}")
    print(f"  digest {result.digest}")
    for doc in result.docs:
        print(f"  doc {doc}")
    return 0


def _print_shape(status: dict[str, Any]) -> None:
    shape = status.get("shape")
    if not shape:
        print("no shape")
        return
    print(
        f"{shape['id']}  {shape['project']}  {shape['state']}  "
        f"attempt {shape.get('attempt', 1)}  {shape.get('branch', '')}"
    )
    if shape.get("error"):
        print(f"  error: {shape['error']}")
    questions = status.get("questions") or {}
    for question in questions.get("questions", []):
        answer = (question.get("answer") or "").strip() or "(unanswered)"
        print(
            f"  {question['id']}  {question['question']}  "
            f"[default: {question.get('default', '')}]  {answer}"
        )
    check = shape.get("check") or {}
    if check:
        print(f"  check: {'ok' if check.get('ok') else 'failed'}")
        for issue in check.get("issues", []):
            print(f"    {issue}")


def _wait_shape_job(daemon: DaemonClient, project: str, job: dict[str, Any]) -> int:
    """Tail one queued turn; the operator sees the same reply the page shows."""
    job_id = job["id"]
    while True:
        current = next(
            (row for row in daemon.shape_jobs(project) if row["id"] == job_id), None
        )
        if current is not None and current["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(1.0)
    status = daemon.shape(project).get("shape") or {}
    if current["status"] != "done":
        print(f"{current['status']}: {current.get('error') or 'cancelled'}")
        return 1
    reply = (current.get("reply") or "").strip()
    if reply:
        print(reply)
    print(f"state {status.get('state')}  attempt {status.get('attempt')}")
    return 0 if status.get("state") not in {"error"} else 1


def _cmd_shape(daemon: DaemonClient, args: argparse.Namespace) -> int:
    command = args.shape_command
    if command == "start":
        record = daemon.shape_start(
            args.project,
            args.rewrite,
            objective=args.objective,
            task=args.task,
            model=args.model,
        )
        print(f"started {record['id']} on {record['project']}")
        if record.get("model"):
            print(f"  inquisitor model {record['model']}")
        print(f"  run cc shape status {record['project']}")
        return 0
    if command == "model":
        daemon.shape_model(args.project, args.model)
        print(f"{args.project}: inquisitor model {args.model}")
        return 0
    if command == "status":
        status = daemon.shape(args.project)
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            _print_shape(status)
        return 0
    if command == "answer":
        job = daemon.shape_enqueue(
            args.project, "answers", answers={args.question_id: args.answer}
        )
        print(f"queued {job['title']} ({job['id']})")
        return 0 if args.no_wait else _wait_shape_job(daemon, args.project, job)
    if command == "revise":
        job = daemon.shape_enqueue(args.project, "revise", message=args.instruction)
        print(f"queued {job['title']} ({job['id']})")
        return 0 if args.no_wait else _wait_shape_job(daemon, args.project, job)
    if command == "jobs":
        jobs = daemon.shape_jobs(args.project)
        if args.json:
            print(json.dumps(jobs, indent=2))
            return 0
        if not jobs:
            print("queue empty")
            return 0
        for job in jobs:
            print(f"{job['id']}  {job['status']:<8} {job['title']}")
        return 0
    if command == "check":
        result = daemon.shape_check(args.project)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_shape(result)
        return 0 if result["check"]["ok"] else 1
    if command == "materialize":
        record = daemon.shape_materialize(args.project)
        print(f"materialized {record['id']}: {record['state']}")
        return 0
    if command == "approve":
        record = daemon.shape_approve(args.project)
        print(f"approved {record['id']} at {record.get('approved_commit')}")
        return 0
    if command == "cancel":
        record = daemon.shape_cancel(args.project)
        print(f"cancelled {record['id']}")
        return 0
    if command == "diff":
        result = daemon.shape_diff(args.project)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result.get("patch", ""))
        return 0
    raise CommandCenterError(f"unknown shape command {command}")


def _cmd_spec_check(taskstore: TaskStoreClient, conn: Any, args: argparse.Namespace) -> int:
    from .paths import project_root

    project = taskstore.get_project(args.project)
    root = project_root(project.path)
    if root is None:
        raise CommandCenterError("project has no checkout")
    active = db.active_shape_for_project(conn, args.project)
    if active is not None:
        worktree = Path(active.get("worktree", ""))
        if worktree.is_dir():
            root = worktree
    require_materialized = (
        active is not None
        and (
            active.get("state") in {"materialized", "approving"}
            or bool((active.get("check") or {}).get("materialized"))
        )
    ) or (active is None and (root / "command-center.yaml").is_file())
    result = check_shape(root, require_materialized=require_materialized)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"spec check: {'ok' if result.ok else 'failed'}")
        print(f"  materialized: {result.materialized}")
        print(f"  docs: {', '.join(result.docs) or '-'}")
        for issue in result.issues:
            print(f"  - {issue}")
    return 0 if result.ok else 1


def _run(
    args: argparse.Namespace,
    config: Config,
    conn: Any,
    taskstore: TaskStoreClient,
    daemon: DaemonClient,
) -> int:
    command = args.command
    if command == "models":
        return _cmd_models(config, args.json)
    if command == "focus":
        return _cmd_focus(daemon, args)
    if command == "agents":
        return _cmd_agents(config, args)
    if command == "run":
        return _cmd_run(daemon, args)
    if command == "runs":
        return _cmd_runs(daemon, args)
    if command == "cancel":
        return _cmd_cancel(daemon, args)
    if command == "agenda":
        return _cmd_agenda(taskstore, daemon, args)
    if command == "spec":
        return _cmd_spec(taskstore, conn, args)
    if command == "shape":
        return _cmd_shape(daemon, args)
    eligibility = EligibilityService(conn, taskstore, config)
    if command in ("status", "tasks"):
        checks = eligibility.check_all()
        if command == "status":
            return _cmd_status(checks, args.json)
        return _cmd_tasks(checks, args.project, args.all, args.json)
    raise CommandCenterError(f"unknown command {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"command-center {__version__}")
        return 0
    conn = None
    try:
        config = load_config(args.config)
        conn = db.connect(config.state_dir)
        with TaskStoreClient(config.taskstore.url) as taskstore, DaemonClient(config) as daemon:
            return _run(args, config, conn, taskstore, daemon)
    except TaskStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.payload and "current_rev" in exc.payload:
            print(f"  current_rev: {exc.payload['current_rev']}", file=sys.stderr)
        return 1
    except CommandCenterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, DaemonError) and exc.payload:
            issues = exc.payload.get("issues")
            if isinstance(issues, list):
                for issue in issues:
                    if isinstance(issue, dict):
                        prefix = f"{issue.get('item_id')}: " if issue.get("item_id") else ""
                        print(
                            f"  {prefix}{issue.get('code')}: {issue.get('detail')}",
                            file=sys.stderr,
                        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
