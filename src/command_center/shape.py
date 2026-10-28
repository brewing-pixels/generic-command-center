from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from ruamel.yaml import YAML

from . import db, gitutil, spec
from .agent import OpenCodeAgent
from .config import Config
from .errors import ShapeError, TaskStoreError
from .events import EventBus
from .paths import project_root
from .plan import Gate, load_plan
from .probe import ModelProbe
from .prompt import (
    build_c4_prompt,
    build_discuss_prompt,
    build_inquisitor_prompt,
    build_repair_prompt,
    build_use_cases_prompt,
)
from .taskstore import TaskStoreClient, TaskStoreProject

TASKS_DOC = "docs/spec/TASKS.yaml"
LEGACY_TASKS_DOC = "TASKS.yaml"
QUESTIONS_DOC = "docs/spec/QUESTIONS.yaml"
USE_CASES_DOC = "docs/spec/USE_CASES.yaml"
C4_DOC = "docs/spec/C4.yaml"
LEGACY_QUESTIONS_DOC = "QUESTIONS.yaml"
GATE_DOC = "command-center.yaml"
TODO_RE = re.compile(r"\b(?:TBD|TODO)\b")
BUSY_STATES = frozenset({"drafting", "revising"})
TREE_EDITABLE = frozenset(
    {"questions", "answers_ready", "ready", "check_failed", "error"}
)
MAX_REPAIRS = 2


def _normalise_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _yaml_read(path: Path) -> Any:
    yaml = YAML(typ="safe")
    try:
        return yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise ShapeError(f"{path}: {exc}") from exc


def _yaml_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML(typ="safe")
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


class ShapeQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^q[0-9A-Za-z_-]+$")
    question: str = Field(min_length=1)
    default: str = ""
    answer: str | None = None
    note: str | None = None


class ShapeQuestions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    questions: list[ShapeQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _version(self) -> ShapeQuestions:
        if self.version != 1:
            raise ValueError(f"unsupported questions version {self.version}")
        return self

    @model_validator(mode="after")
    def _unique_ids(self) -> ShapeQuestions:
        seen: set[str] = set()
        for question in self.questions:
            if question.id in seen:
                raise ValueError(f"duplicate question id {question.id!r}")
            seen.add(question.id)
        return self

    @property
    def unanswered(self) -> list[ShapeQuestion]:
        return [
            question
            for question in self.questions
            if not (question.answer or "").strip()
        ]


class UseCaseBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guard: str = Field(min_length=1)
    to: str = Field(min_length=1)

    @model_validator(mode="after")
    def _guard(self) -> UseCaseBranch:
        if not self.guard.strip():
            raise ValueError("branch guard cannot be blank")
        return self


class UseCaseStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, pattern=r"^s[0-9A-Za-z_-]+$")
    label: str = Field(min_length=1)
    task: str | None = None
    kind: Literal["activity", "choice"] = "activity"
    branches: list[UseCaseBranch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _label(self) -> UseCaseStep:
        if not self.label.strip():
            raise ValueError("step label cannot be blank")
        if self.branches and self.kind != "choice":
            raise ValueError(
                "only a choice step can carry branches; set kind: choice"
            )
        return self


class UseCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^uc[0-9A-Za-z_-]+$")
    title: str = Field(min_length=1)
    actor: str | None = None
    summary: str | None = None
    steps: list[UseCaseStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _steps(self) -> UseCase:
        seen: set[str] = set()
        positions: dict[str, int] = {}
        for index, step in enumerate(self.steps):
            if step.id is None:
                continue
            if step.id in seen:
                raise ValueError(f"duplicate step id {step.id!r}")
            seen.add(step.id)
            positions[step.id] = index
        for index, step in enumerate(self.steps):
            for branch in step.branches:
                target = positions.get(branch.to)
                if target is None:
                    raise ValueError(
                        f"step {step.label!r} branches to unknown step "
                        f"{branch.to!r}"
                    )
                if target <= index:
                    raise ValueError(
                        f"step {step.label!r} branches backwards to "
                        f"{step.id!r}; branches must point at a later step"
                    )
        return self


class ShapeUseCases(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    use_cases: list[UseCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def _version(self) -> ShapeUseCases:
        if self.version != 1:
            raise ValueError(f"unsupported use cases version {self.version}")
        return self

    @model_validator(mode="after")
    def _unique_ids(self) -> ShapeUseCases:
        seen: set[str] = set()
        for use_case in self.use_cases:
            if use_case.id in seen:
                raise ValueError(f"duplicate use case id {use_case.id!r}")
            seen.add(use_case.id)
        return self


C4_ELEMENT_TYPES = (
    "person",
    "system",
    "external",
    "container",
    "component",
    "database",
    "queue",
    "code",
    "decision",
)


class C4Element(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: Literal[
        "person",
        "system",
        "external",
        "container",
        "component",
        "database",
        "queue",
        "code",
        "decision",
    ] = "system"
    name: str = Field(min_length=1)
    technology: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _name(self) -> C4Element:
        if not self.name.strip():
            raise ValueError("element name cannot be blank")
        return self


class C4Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    label: str = Field(min_length=1)
    technology: str | None = None

    @model_validator(mode="after")
    def _label(self) -> C4Relationship:
        if not self.label.strip():
            raise ValueError("relationship label cannot be blank")
        return self


class C4Level(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elements: list[C4Element] = Field(default_factory=list)
    relationships: list[C4Relationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> C4Level:
        ids: set[str] = set()
        for element in self.elements:
            if element.id in ids:
                raise ValueError(f"duplicate element id {element.id!r}")
            ids.add(element.id)
        for relationship in self.relationships:
            for end in (relationship.from_, relationship.to):
                if end not in ids:
                    raise ValueError(
                        f"relationship {relationship.label!r} names unknown "
                        f"element {end!r}"
                    )
        return self


class C4Levels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: C4Level = Field(default_factory=C4Level)
    container: C4Level = Field(default_factory=C4Level)
    component: C4Level = Field(default_factory=C4Level)
    code: C4Level = Field(default_factory=C4Level)


class ShapeC4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    levels: C4Levels = Field(default_factory=C4Levels)

    @model_validator(mode="after")
    def _version(self) -> ShapeC4:
        if self.version != 1:
            raise ValueError(f"unsupported C4 version {self.version}")
        return self


class ShapeTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    detail: str | None = None
    acceptance: list[str] = Field(min_length=1)
    gate: Gate
    depends_on: list[str]
    scope: list[str]
    taskstore_id: str | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> ShapeTask:
        if any(not item.strip() for item in self.acceptance):
            raise ValueError("acceptance entries cannot be empty")
        if any(not item.strip() for item in self.scope):
            raise ValueError("scope entries cannot be empty")
        return self


class ShapeObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    detail: str | None = None
    acceptance: list[str] = Field(min_length=1)
    gate: Gate
    tasks: list[ShapeTask] = Field(min_length=1)
    taskstore_id: str | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> ShapeObjective:
        if any(not item.strip() for item in self.acceptance):
            raise ValueError("acceptance entries cannot be empty")
        return self


class ShapeTasks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    objectives: list[ShapeObjective] = Field(min_length=1)

    @model_validator(mode="after")
    def _version(self) -> ShapeTasks:
        if self.version != 1:
            raise ValueError(f"unsupported tasks version {self.version}")
        return self

    @model_validator(mode="after")
    def _unique_and_acyclic(self) -> ShapeTasks:
        objective_slugs: set[str] = set()
        task_slugs: dict[str, ShapeTask] = {}
        for objective in self.objectives:
            if objective.slug in objective_slugs:
                raise ValueError(f"duplicate objective slug {objective.slug!r}")
            objective_slugs.add(objective.slug)
            for task in objective.tasks:
                if task.slug in task_slugs:
                    raise ValueError(f"duplicate task slug {task.slug!r}")
                task_slugs[task.slug] = task
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(slug: str) -> None:
            if slug in visited:
                return
            if slug in visiting:
                raise ValueError(f"dependency cycle at {slug!r}")
            task = task_slugs.get(slug)
            if task is None:
                raise ValueError(f"unknown dependency {slug!r}")
            visiting.add(slug)
            for dependency in task.depends_on:
                visit(dependency)
            visiting.remove(slug)
            visited.add(slug)

        for task in task_slugs.values():
            for dependency in task.depends_on:
                if dependency not in task_slugs:
                    raise ValueError(
                        f"task {task.slug!r} depends on unknown {dependency!r}"
                    )
            visit(task.slug)
        return self


@dataclass
class ShapeCheck:
    ok: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    materialized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "warnings": self.warnings,
            "docs": self.docs,
            "materialized": self.materialized,
        }


def _c4_element_count(c4: ShapeC4) -> int:
    levels = c4.levels
    return sum(
        len(level.elements)
        for level in (
            levels.context,
            levels.container,
            levels.component,
            levels.code,
        )
    )


def tasks_path(root: Path) -> Path:
    canonical = root / TASKS_DOC
    if canonical.is_file():
        return canonical
    legacy = root / LEGACY_TASKS_DOC
    return legacy if legacy.is_file() else canonical


def questions_path(root: Path) -> Path:
    canonical = root / QUESTIONS_DOC
    if canonical.is_file():
        return canonical
    legacy = root / LEGACY_QUESTIONS_DOC
    return legacy if legacy.is_file() else canonical


def load_tasks(root: Path) -> ShapeTasks:
    path = tasks_path(root)
    if not path.is_file():
        raise ShapeError(f"{TASKS_DOC} is missing")
    data = _yaml_read(path)
    if not isinstance(data, dict):
        raise ShapeError(f"{path}: expected a YAML mapping")
    try:
        return ShapeTasks.model_validate(data)
    except ValidationError as exc:
        raise ShapeError(f"{path}: {exc}") from exc


def write_tasks(root: Path, tasks: ShapeTasks) -> None:
    path = root / TASKS_DOC
    _yaml_write(path, tasks.model_dump(mode="json", exclude_none=False))


def load_questions(root: Path) -> ShapeQuestions:
    path = questions_path(root)
    if not path.is_file():
        raise ShapeError(f"{QUESTIONS_DOC} is missing")
    data = _yaml_read(path)
    if not isinstance(data, dict):
        raise ShapeError(f"{path}: expected a YAML mapping")
    try:
        return ShapeQuestions.model_validate(data)
    except ValidationError as exc:
        raise ShapeError(f"{path}: {exc}") from exc


def write_questions(root: Path, questions: ShapeQuestions) -> None:
    path = root / QUESTIONS_DOC
    _yaml_write(path, questions.model_dump(mode="json", exclude_none=False))


def parse_use_cases(text: str, source: str = USE_CASES_DOC) -> ShapeUseCases:
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise ShapeError(f"{source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ShapeError(f"{source}: expected a YAML mapping")
    try:
        return ShapeUseCases.model_validate(data)
    except ValidationError as exc:
        raise ShapeError(f"{source}: {exc}") from exc


def load_use_cases(root: Path) -> ShapeUseCases | None:
    """Optional artefact: a shape without user-facing flows has no file."""
    path = root / USE_CASES_DOC
    if not path.is_file():
        return None
    return parse_use_cases(path.read_text(encoding="utf-8"), str(path))


def write_use_cases(root: Path, use_cases: ShapeUseCases) -> None:
    """Write the file without the defaults a reader would fill back in."""
    path = root / USE_CASES_DOC
    data = use_cases.model_dump(mode="json", exclude_none=True)
    for use_case in data.get("use_cases", []):
        for step in use_case.get("steps", []):
            if step.get("kind") == "activity":
                step.pop("kind", None)
            if not step.get("branches"):
                step.pop("branches", None)
    _yaml_write(path, data)


def parse_c4(text: str, source: str = C4_DOC) -> ShapeC4:
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise ShapeError(f"{source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ShapeError(f"{source}: expected a YAML mapping")
    try:
        return ShapeC4.model_validate(data)
    except ValidationError as exc:
        raise ShapeError(f"{source}: {exc}") from exc


def load_c4(root: Path) -> ShapeC4 | None:
    """Optional artefact: a shape without an architecture has no C4 file."""
    path = root / C4_DOC
    if not path.is_file():
        return None
    return parse_c4(path.read_text(encoding="utf-8"), str(path))


def write_c4(root: Path, c4: ShapeC4) -> None:
    path = root / C4_DOC
    _yaml_write(
        path,
        c4.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def use_case_task_issues(
    use_cases: ShapeUseCases, tasks: ShapeTasks | None
) -> list[str]:
    if tasks is None:
        return []
    slugs = {
        task.slug for objective in tasks.objectives for task in objective.tasks
    }
    issues: list[str] = []
    for use_case in use_cases.use_cases:
        for step in use_case.steps:
            if step.task and step.task not in slugs:
                issues.append(
                    f"use case {use_case.id!r} step {step.label!r} names unknown "
                    f"task {step.task!r}"
                )
    return issues


def answer_question(root: Path, question_id: str, answer: str) -> ShapeQuestions:
    questions = load_questions(root)
    found = False
    for question in questions.questions:
        if question.id == question_id:
            question.answer = answer.strip()
            found = True
            break
    if not found:
        raise ShapeError(f"unknown question {question_id!r}")
    if not questions.questions:
        raise ShapeError("QUESTIONS.yaml has no questions")
    write_questions(root, questions)
    return questions


def _scan_todos(path: Path, issues: list[str]) -> None:
    if not path.is_file():
        return
    in_fence = False
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and TODO_RE.search(line):
            issues.append(f"{path.name}:{number}: open TBD/TODO")


def _check_materialized(
    root: Path, tasks: ShapeTasks, issues: list[str]
) -> None:
    try:
        plan = load_plan(root)
    except Exception as exc:  # noqa: BLE001 - report the parse error
        issues.append(f"command-center.yaml: {exc}")
        return
    task_map: dict[str, str] = {}
    for objective in tasks.objectives:
        if not objective.taskstore_id:
            issues.append(f"objective {objective.slug!r} has no taskstore_id")
            continue
        plan_objective = plan.objectives.get(objective.taskstore_id)
        if plan_objective is None or not plan_objective.gate:
            issues.append(
                f"objective {objective.slug!r} has no gate in command-center.yaml"
            )
        for task in objective.tasks:
            if not task.taskstore_id:
                issues.append(f"task {task.slug!r} has no taskstore_id")
                continue
            task_map[task.slug] = task.taskstore_id
    for objective in tasks.objectives:
        for task in objective.tasks:
            if not task.taskstore_id:
                continue
            plan_task = plan.tasks.get(task.taskstore_id)
            if plan_task is None or not plan_task.gate:
                issues.append(
                    f"task {task.slug!r} has no gate in command-center.yaml"
                )
                continue
            expected_deps = {
                task_map[dependency]
                for dependency in task.depends_on
                if dependency in task_map
            }
            if set(plan_task.depends_on) != expected_deps:
                issues.append(
                    f"task {task.slug!r} depends_on does not match the gate file"
                )
            if plan_task.scope and set(plan_task.scope) != set(task.scope):
                issues.append(
                    f"task {task.slug!r} scope does not match the gate file"
                )


def check_shape(root: Path, *, require_materialized: bool = False) -> ShapeCheck:
    issues: list[str] = []
    warnings: list[str] = []
    root = Path(root)
    if not gitutil.is_git_repo(root):
        return ShapeCheck(False, ["project is not a git repository"], [], [], False)
    if not gitutil.is_clean(root):
        issues.append("shape worktree has uncommitted or untracked files")
    docs = spec.default_docs(root)
    for doc in docs:
        path = root / doc
        if not path.is_file():
            issues.append(f"{doc} is missing")
            continue
        if not gitutil.file_tracked(root, doc):
            issues.append(f"{doc} is not committed")
        _scan_todos(path, issues)
    tasks: ShapeTasks | None = None
    try:
        tasks = load_tasks(root)
    except ShapeError as exc:
        issues.append(str(exc))
    if tasks is not None:
        _scan_todos(tasks_path(root), issues)
        # Gates, acceptance, dependencies, and cycles are enforced by pydantic.
        if not gitutil.file_tracked(
            root, str(tasks_path(root).relative_to(root))
        ):
            issues.append(f"{TASKS_DOC} is not committed")
    try:
        questions = load_questions(root)
    except ShapeError as exc:
        questions = None
        issues.append(str(exc))
    if questions is not None:
        _scan_todos(questions_path(root), issues)
        for question in questions.unanswered:
            issues.append(f"question {question.id!r} has no answer")
        if not gitutil.file_tracked(
            root, str(questions_path(root).relative_to(root))
        ):
            issues.append(f"{QUESTIONS_DOC} is not committed")
    try:
        use_cases = load_use_cases(root)
    except ShapeError as exc:
        use_cases = None
        issues.append(str(exc))
    if use_cases is not None:
        _scan_todos(root / USE_CASES_DOC, issues)
        issues.extend(use_case_task_issues(use_cases, tasks))
        if not gitutil.file_tracked(root, USE_CASES_DOC):
            issues.append(f"{USE_CASES_DOC} is not committed")
    try:
        c4 = load_c4(root)
    except ShapeError as exc:
        c4 = None
        issues.append(str(exc))
    if c4 is not None:
        _scan_todos(root / C4_DOC, issues)
        if not gitutil.file_tracked(root, C4_DOC):
            issues.append(f"{C4_DOC} is not committed")
    materialized = bool(
        require_materialized
        or (root / GATE_DOC).is_file()
        and tasks is not None
        and all(
            objective.taskstore_id for objective in tasks.objectives
        )
    )
    if tasks is not None and materialized:
        _check_materialized(root, tasks, issues)
        gate_path = root / GATE_DOC
        if not gate_path.is_file():
            issues.append("command-center.yaml is missing")
        elif not gitutil.file_tracked(root, GATE_DOC):
            issues.append("command-center.yaml is not committed")
    if require_materialized and not materialized:
        issues.append("decomposition is not materialized in TaskStore")
    return ShapeCheck(
        not issues,
        issues,
        warnings,
        [doc for doc in docs if (root / doc).is_file()],
        materialized,
    )


def task_rows(tasks: ShapeTasks) -> list[tuple[ShapeObjective, ShapeTask]]:
    return [
        (objective, task)
        for objective in tasks.objectives
        for task in objective.tasks
    ]


class ShapeRecord(BaseModel):
    id: str
    project: str
    branch: str
    worktree: str
    run_dir: str
    base_sha: str
    state: str = "drafting"
    attempt: int = 1
    rewrite: bool = False
    objective_id: str | None = None
    task_id: str | None = None
    previous_spec_state: dict[str, Any] | None = None
    model: str | None = None
    session_id: str | None = None
    critique_session_id: str | None = None
    last_reply: str | None = None
    last_usage: dict[str, Any] | None = None
    approved_commit: str | None = None
    error: str | None = None
    check: dict[str, Any] | None = None
    created: datetime
    updated: datetime

    def save(self, conn) -> None:
        self.updated = _utcnow()
        db.save_shape(conn, self.model_dump(mode="json"))

    @classmethod
    def load(cls, conn, shape_id: str) -> ShapeRecord | None:
        data = db.get_shape(conn, shape_id)
        return cls.model_validate(data) if data else None

    @classmethod
    def new(cls, **kwargs: Any) -> ShapeRecord:
        now = _utcnow()
        return cls(id=f"s_{secrets.token_hex(4)}", created=now, updated=now, **kwargs)


def _shape_questions(root: Path) -> ShapeQuestions | None:
    try:
        return load_questions(root)
    except ShapeError:
        return None


def _validation_text(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "tree"
        lines.append(f"{where}: {error.get('msg', 'invalid')}")
    return "; ".join(lines) or "invalid tree"


def _repairable(check: ShapeCheck) -> bool:
    """Unanswered questions are operator work, not something the agent can fix."""
    if not check.issues:
        return False
    return any("has no answer" not in issue for issue in check.issues)


class ShapeEngine:
    def __init__(
        self,
        config: Config,
        taskstore: TaskStoreClient,
        *,
        agent: Any | None = None,
        bus: EventBus | None = None,
    ):
        self.config = config
        self.taskstore = taskstore
        self.agent = agent or OpenCodeAgent(config)
        self.bus = bus or EventBus()

    def _conn(self):
        return db.connect(self.config.state_dir)

    def _publish(self, event: str, data: dict[str, Any]) -> None:
        self.bus.publish(event, data)

    def _inquisitor(
        self, record: ShapeRecord | None = None
    ) -> tuple[str, Any]:
        """The model id and config for this shape, or the role default."""
        model_id = (record.model if record is not None else None) or (
            self.config.roles.get("inquisitor")
        )
        if not model_id:
            raise ShapeError("no inquisitor model configured")
        model = self.config.models.get(model_id)
        if model is None:
            raise ShapeError(f"unknown inquisitor model {model_id!r}")
        status = ModelProbe(self.config).check(model_id)
        if status is None or not status.up:
            reason = status.reason if status else "model unavailable"
            raise ShapeError(f"inquisitor model {model_id} is down: {reason}")
        return model_id, model

    def set_model(self, project_id: str, model_id: str) -> dict[str, Any]:
        """Pick the inquisitor model for this shape, mid-session allowed."""
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        if record.state in {"approved", "cancelled"}:
            raise ShapeError(f"shape is {record.state}")
        model = self.config.models.get(model_id)
        if model is None:
            raise ShapeError(f"unknown model {model_id!r}")
        status = ModelProbe(self.config).check(model_id)
        if status is None or not status.up:
            reason = status.reason if status else "model unavailable"
            raise ShapeError(f"model {model_id} is down: {reason}")
        context = int((record.last_usage or {}).get("context") or 0)
        if model.context_window and context > model.context_window:
            raise ShapeError(
                f"{model.label} has a {model.context_window} token window but this "
                f"session already holds {context}; approve or split before switching"
            )
        conn = self._conn()
        try:
            record.model = model_id
            record.save(conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": project_id,
                    "state": record.state,
                },
            )
            return self.tree(project_id)
        finally:
            conn.close()

    def _active(self, conn, project_id: str) -> ShapeRecord:
        data = db.active_shape_for_project(conn, project_id)
        if data is None:
            raise ShapeError(f"no active shape for {project_id}")
        return ShapeRecord.model_validate(data)

    def _latest(self, conn, project_id: str) -> ShapeRecord:
        data = db.latest_shape(conn, project_id)
        if data is None:
            raise ShapeError(f"no shape for {project_id}")
        return ShapeRecord.model_validate(data)

    def _stale_after(self) -> float:
        """A run that outlived its agent timeout cannot still be alive."""
        return float(self.config.budgets.agent_timeout_seconds + 300)

    def _is_stale(self, record: ShapeRecord) -> bool:
        updated = record.updated
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return (_utcnow() - updated).total_seconds() > self._stale_after()

    def _claim(self, conn, record: ShapeRecord, *, to_state: str) -> ShapeRecord:
        """Move a shape out of an idle state; only one caller can win."""
        fresh = ShapeRecord.load(conn, record.id)
        if fresh is None:
            raise ShapeError(f"shape {record.id} not found")
        if fresh.state in BUSY_STATES and not self._is_stale(fresh):
            raise ShapeError(
                f"shape {fresh.id} is {fresh.state}; one inquisitor at a time — "
                "wait for it to finish or cancel the shape"
            )
        claims = (
            "drafting",
            "revising",
            "questions",
            "answers_ready",
            "ready",
            "check_failed",
            "error",
        )
        if not db.claim_shape(conn, fresh.id, from_states=claims, to_state=to_state):
            raise ShapeError(
                f"shape {fresh.id} is already running; one inquisitor at a time"
            )
        fresh.state = to_state
        return fresh

    def assert_idle(self, project_id: str) -> ShapeRecord:
        """Refuse to start a second inquisitor on a project that is shaping."""
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            if record.state in BUSY_STATES and not self._is_stale(record):
                raise ShapeError(
                    f"shape {record.id} is {record.state}; one inquisitor at a time"
                )
            return record
        finally:
            conn.close()

    def prepare(
        self,
        project_id: str,
        rewrite: bool = False,
        *,
        objective_id: str | None = None,
        task_id: str | None = None,
        model: str | None = None,
    ) -> ShapeRecord:
        project = self.taskstore.get_project(project_id)
        if project.status in {"parked", "archived"}:
            raise ShapeError(f"project {project_id} is {project.status}")
        seed_objective = None
        seed_task = None
        if task_id is not None and objective_id is None:
            seed_objective = next(
                (
                    objective
                    for objective in project.objectives
                    if any(task.id == task_id for task in objective.tasks)
                ),
                None,
            )
            if seed_objective is None:
                raise ShapeError(
                    f"task {task_id!r} not found in project {project_id}"
                )
            objective_id = seed_objective.id
        if objective_id is not None:
            seed_objective = next(
                (objective for objective in project.objectives if objective.id == objective_id),
                None,
            )
            if seed_objective is None:
                raise ShapeError(
                    f"objective {objective_id!r} not found in project {project_id}"
                )
            if task_id is not None:
                seed_task = next(
                    (task for task in seed_objective.tasks if task.id == task_id),
                    None,
                )
                if seed_task is None:
                    raise ShapeError(
                        f"task {task_id!r} not found in objective {objective_id}"
                    )
        root = project_root(project.path)
        if root is None or not root.is_dir():
            raise ShapeError(f"project {project_id} has no checkout")
        try:
            if not gitutil.is_git_repo(root):
                raise ShapeError(f"{root} is not a git repository")
            if not gitutil.is_clean(root):
                raise ShapeError("base checkout is dirty")
        except gitutil.GitError as exc:
            raise ShapeError(str(exc)) from exc
        conn = self._conn()
        try:
            active = db.active_shape_for_project(conn, project_id)
            if active:
                raise ShapeError(
                    f"shape {active['id']} is already active for {project_id}"
                )
            run = db.active_run_for_project(conn, project_id)
            if run:
                raise ShapeError(
                    f"run {run['id']} is active for {project_id}"
                )
            base = gitutil.ensure_integration(root)
            number = 1
            while gitutil.branch_exists(root, f"shape-{number}"):
                number += 1
            branch = f"shape-{number}"
            worktree = self.config.worktree_root / project_id / branch
            if worktree.exists():
                gitutil.worktree_remove(root, worktree)
            gitutil.worktree_add(root, worktree, branch, base)
            if model is not None and model not in self.config.models:
                raise ShapeError(f"unknown model {model!r}")
            record = ShapeRecord.new(
                project=project_id,
                model=model,
                branch=branch,
                worktree=str(worktree),
                run_dir=str(self.config.state_dir / "shapes" / "pending"),
                base_sha=base,
                rewrite=rewrite,
                objective_id=objective_id,
                task_id=task_id,
                previous_spec_state=(
                    project.spec_state.model_dump(mode="json")
                    if project.spec_state
                    else None
                ),
            )
            record.run_dir = str(self.config.state_dir / "shapes" / record.id)
            Path(record.run_dir).mkdir(parents=True, exist_ok=True)
            previous = dict(record.previous_spec_state or {})
            previous.setdefault("docs", [])
            previous.setdefault("digest", "")
            previous.setdefault("dirty", False)
            previous["phase"] = "shape"
            try:
                self._patch_spec_state(project_id, previous)
            except Exception:
                gitutil.worktree_remove(root, worktree)
                raise
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": project_id, "state": record.state},
            )
            return record
        finally:
            conn.close()

    def _patch_spec_state(
        self, project_id: str, state: dict[str, Any]
    ) -> TaskStoreProject:
        project = self.taskstore.get_project(project_id)
        payload = {"spec_state": state, "if_rev": project.rev}
        try:
            return self.taskstore.patch_project(project_id, payload)
        except TaskStoreError as exc:
            if exc.status_code != 409:
                raise
            latest = self.taskstore.get_project(project_id)
            payload["if_rev"] = latest.rev
            return self.taskstore.patch_project(project_id, payload)

    def _commit(self, root: Path, message: str) -> str:
        try:
            gitutil.add_all(root)
            if gitutil.status_short(root).strip():
                return gitutil.commit(root, message)
            return gitutil.head_commit(root, "HEAD")
        except gitutil.GitError as exc:
            raise ShapeError(str(exc)) from exc

    def _finish_state(
        self, record: ShapeRecord, check: ShapeCheck, conn
    ) -> None:
        record.check = check.to_dict()
        questions = _shape_questions(Path(record.worktree))
        if questions is not None and questions.unanswered:
            record.state = "questions"
        elif check.ok:
            record.state = "ready"
        else:
            record.state = "check_failed"
        record.save(conn)

    def _seed(self, record: ShapeRecord, project):
        objective = next(
            (
                item
                for item in project.objectives
                if item.id == record.objective_id
            ),
            None,
        )
        task = None
        if objective is not None and record.task_id is not None:
            task = next(
                (item for item in objective.tasks if item.id == record.task_id),
                None,
            )
        return objective, task

    def _adopt_existing(self, record: ShapeRecord, root: Path) -> None:
        """Attach existing TaskStore IDs to matching titles before materialization."""
        project = self.taskstore.get_project(record.project)
        tasks = load_tasks(root)
        for objective in tasks.objectives:
            objective.taskstore_id = None
            for task in objective.tasks:
                task.taskstore_id = None

        by_objective_title: dict[str, list[Any]] = {}
        for objective in project.objectives:
            by_objective_title.setdefault(
                _normalise_title(objective.title), []
            ).append(objective)
        used_objectives: set[str] = set()
        for objective in tasks.objectives:
            matches = by_objective_title.get(_normalise_title(objective.title), [])
            if len(matches) == 1 and matches[0].id not in used_objectives:
                objective.taskstore_id = matches[0].id
                used_objectives.add(matches[0].id)

        selected_objective = next(
            (
                objective
                for objective in project.objectives
                if objective.id == record.objective_id
            ),
            None,
        )
        if (
            selected_objective is not None
            and selected_objective.id not in used_objectives
        ):
            selected_titles = {
                _normalise_title(task.title) for task in selected_objective.tasks
            }
            candidates = [
                (
                    sum(
                        _normalise_title(task.title) in selected_titles
                        for task in objective.tasks
                    ),
                    objective,
                )
                for objective in tasks.objectives
                if objective.taskstore_id is None
            ]
            candidates.sort(key=lambda row: row[0], reverse=True)
            if candidates and candidates[0][0] > 0:
                candidates[0][1].taskstore_id = selected_objective.id
                used_objectives.add(selected_objective.id)

        objectives_by_id = {objective.id: objective for objective in project.objectives}
        used_tasks: set[str] = set()
        for objective in tasks.objectives:
            if not objective.taskstore_id:
                continue
            current = objectives_by_id.get(objective.taskstore_id)
            if current is None:
                objective.taskstore_id = None
                continue
            by_task_title: dict[str, list[Any]] = {}
            for task in current.tasks:
                by_task_title.setdefault(
                    _normalise_title(task.title), []
                ).append(task)
            for task in objective.tasks:
                matches = by_task_title.get(_normalise_title(task.title), [])
                if len(matches) == 1 and matches[0].id not in used_tasks:
                    task.taskstore_id = matches[0].id
                    used_tasks.add(matches[0].id)

        write_tasks(root, tasks)

    def _inquisitor_pass(
        self,
        record: ShapeRecord,
        prompt: str,
        *,
        model: str,
        session_id: str | None,
        append_log: bool = False,
        prompt_name: str = "prompt.txt",
        agent: str | None = None,
    ) -> Any:
        run_dir = Path(record.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / prompt_name).write_text(prompt, encoding="utf-8")
        result = self.agent.run(
            prompt,
            cwd=Path(record.worktree),
            run_dir=run_dir,
            model=model,
            agent=agent or "cc-inquisitor",
            append=append_log,
            session_id=session_id,
        )
        # Keep whatever the turn produced, even when it failed: a dropped
        # connection still leaves a resumable session and a partial reply.
        if result.session_id:
            record.session_id = result.session_id
        record.last_reply = result.final_text
        record.last_usage = {
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost": result.cost,
            "context": result.context,
        }
        if result.exit_code != 0:
            detail = result.error or f"exited {result.exit_code}"
            raise ShapeError(f"inquisitor failed: {detail}")
        return result

    def _settle(
        self,
        record: ShapeRecord,
        conn,
        *,
        label: str,
        model: str,
    ) -> ShapeCheck:
        """Commit the pass, then let the inquisitor repair the check in-session."""
        self._commit(Path(record.worktree), label)
        check = check_shape(Path(record.worktree))
        repairs = 0
        while not check.ok and repairs < MAX_REPAIRS and _repairable(check):
            repairs += 1
            prompt = build_repair_prompt(issues=check.issues, attempt=repairs)
            repair_result = self._inquisitor_pass(
                record,
                prompt,
                model=model,
                session_id=record.session_id,
                append_log=True,
                prompt_name=f"repair-{repairs}.txt",
            )
            record.session_id = repair_result.session_id or record.session_id
            record.save(conn)
            self._commit(Path(record.worktree), f"{label} repair {repairs}")
            check = check_shape(Path(record.worktree))
        return check

    def draft(self, record: ShapeRecord) -> ShapeRecord:
        conn = self._conn()
        try:
            record.state = "drafting"
            record.save(conn)
            _, model_cfg = self._inquisitor(record)
            model = model_cfg.model
            project = self.taskstore.get_project(record.project)
            seed_objective, seed_task = self._seed(record, project)
            prompt = build_inquisitor_prompt(
                project=project,
                root=Path(record.worktree),
                questions=None,
                attempt=record.attempt,
                rewrite=record.rewrite,
                seed_objective=seed_objective,
                seed_task=seed_task,
            )
            pass_result = self._inquisitor_pass(
                record, prompt, model=model, session_id=record.session_id
            )
            record.session_id = pass_result.session_id
            check = self._settle(
                record,
                conn,
                label=f"cc shape {record.id}: draft",
                model=model,
            )
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": record.project, "state": record.state},
            )
            return record
        except Exception as exc:
            record.state = "error"
            record.error = str(exc)
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": record.project, "state": "error"},
            )
            raise
        finally:
            conn.close()

    def answer(
        self,
        project_id: str,
        question_id: str,
        answer: str,
        *,
        in_turn: bool = False,
    ) -> ShapeRecord:
        """Write one answer. `in_turn` is the queue worker: it runs alone, so a
        stale `revising` left by a dead daemon must not block it."""
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            busy = {"drafting", "revising"} if not in_turn else set()
            if record.state in busy | {"approved", "cancelled"}:
                raise ShapeError(f"shape is {record.state}")
            if record.state == "materialized":
                raise ShapeError("shape is already materialized; cancel or approve it")
            questions = answer_question(Path(record.worktree), question_id, answer)
            self._commit(
                Path(record.worktree),
                f"cc shape {record.id}: answer {question_id}",
            )
            record.state = (
                "answers_ready" if not questions.unanswered else "questions"
            )
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": project_id, "state": record.state},
            )
            return record
        finally:
            conn.close()

    def _preflight_revise(
        self, record: ShapeRecord
    ) -> tuple[ShapeRecord, ShapeQuestions | None]:
        """Refusals must not mark the shape as errored."""
        if record.state in {"materialized", "approved", "cancelled"}:
            raise ShapeError(f"shape is {record.state}")
        try:
            questions: ShapeQuestions | None = load_questions(
                Path(record.worktree)
            )
        except ShapeError:
            # An unreadable queue is exactly what a revision has to be able to
            # repair. The inquisitor reads the file itself and the check issues
            # are in the prompt, so do not block here.
            questions = None
        conn = self._conn()
        try:
            return self._claim(conn, record, to_state="revising"), questions
        finally:
            conn.close()

    def revise(
        self, record: ShapeRecord, instruction: str | None = None
    ) -> ShapeRecord:
        record, questions = self._preflight_revise(record)
        conn = self._conn()
        try:
            record.attempt += 1
            record.error = None
            record.save(conn)
            _, model_cfg = self._inquisitor(record)
            model = model_cfg.model
            project = self.taskstore.get_project(record.project)
            seed_objective, seed_task = self._seed(record, project)
            prompt = build_inquisitor_prompt(
                project=project,
                root=Path(record.worktree),
                questions=questions,
                attempt=record.attempt,
                rewrite=record.rewrite,
                seed_objective=seed_objective,
                seed_task=seed_task,
                previous_issues=(record.check or {}).get("issues") or [],
                instruction=instruction,
            )
            pass_result = self._inquisitor_pass(
                record,
                prompt,
                model=model,
                session_id=record.session_id,
                append_log=True,
            )
            record.session_id = pass_result.session_id or record.session_id
            record.critique_session_id = record.session_id
            record.save(conn)
            check = self._settle(
                record,
                conn,
                label=f"cc shape {record.id}: revise {record.attempt}",
                model=model,
            )
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": record.project, "state": record.state},
            )
            return record
        except Exception as exc:
            record.state = "error"
            record.error = str(exc)
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": record.project, "state": "error"},
            )
            raise
        finally:
            conn.close()

    def tree(self, project_id: str) -> dict[str, Any]:
        """The decomposition as data, for the diagram and the tree editor."""
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            root = Path(record.worktree)
            tasks = load_tasks(root)
            check = check_shape(root)
            return {
                "shape": record.id,
                "state": record.state,
                "tree": tasks.model_dump(mode="json"),
                "check": check.to_dict(),
                "editable": record.state in TREE_EDITABLE,
            }
        finally:
            conn.close()

    def save_tree(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a hand-edited decomposition, write it, and re-check."""
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        if record.state in {"drafting", "revising"}:
            raise ShapeError("the inquisitor is working; wait for it to finish")
        if record.state not in TREE_EDITABLE:
            raise ShapeError(f"shape is {record.state}; the tree is read-only")
        try:
            tasks = ShapeTasks.model_validate(payload)
        except ValidationError as exc:
            raise ShapeError(_validation_text(exc)) from exc
        conn = self._conn()
        try:
            root = Path(record.worktree)
            if not root.is_dir():
                raise ShapeError("the shape worktree is gone")
            write_tasks(root, tasks)
            self._commit(root, f"cc shape {record.id}: edit tree")
            check = check_shape(root)
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": project_id,
                    "state": record.state,
                },
            )
            return {
                "shape": record.id,
                "state": record.state,
                "tree": tasks.model_dump(mode="json"),
                "check": check.to_dict(),
                "editable": record.state in TREE_EDITABLE,
            }
        finally:
            conn.close()

    def derive_use_cases(self, record: ShapeRecord) -> ShapeRecord:
        """One turn that writes USE_CASES.yaml from the spec, nothing else."""
        record, _ = self._preflight_revise(record)
        conn = self._conn()
        try:
            root = Path(record.worktree)
            _, model_cfg = self._inquisitor(record)
            project = self.taskstore.get_project(record.project)
            tasks: ShapeTasks | None
            try:
                tasks = load_tasks(root)
            except ShapeError:
                tasks = None
            existing = None
            existing_text = None
            path = root / USE_CASES_DOC
            if path.is_file():
                existing_text = path.read_text(encoding="utf-8")
                try:
                    existing = load_use_cases(root)
                except ShapeError:
                    # A broken file is exactly what an update should repair.
                    existing = None
            prompt = build_use_cases_prompt(
                project=project,
                root=root,
                tasks=tasks,
                existing=existing_text,
            )
            pass_result = self._inquisitor_pass(
                record,
                prompt,
                model=model_cfg.model,
                session_id=record.session_id,
                append_log=True,
                prompt_name="use-cases.txt",
            )
            record.session_id = pass_result.session_id or record.session_id
            record.save(conn)
            doc = load_use_cases(root)
            if doc is None:
                raise ShapeError("the inquisitor did not write USE_CASES.yaml")
            if existing is not None and existing.use_cases and not doc.use_cases:
                raise ShapeError(
                    "the update removed every use case; the file is left "
                    "uncommitted for you to fix"
                )
            issues = use_case_task_issues(doc, tasks)
            if issues:
                raise ShapeError("; ".join(issues))
            self._commit(root, f"cc shape {record.id}: use cases from spec")
            check = check_shape(root)
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": record.project,
                    "state": record.state,
                },
            )
            return record
        except Exception as exc:
            record.state = "error"
            record.error = str(exc)
            record.save(conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": record.project,
                    "state": "error",
                },
            )
            raise
        finally:
            conn.close()

    def derive_c4(self, record: ShapeRecord) -> ShapeRecord:
        """One turn that writes C4.yaml from the spec, nothing else."""
        record, _ = self._preflight_revise(record)
        conn = self._conn()
        try:
            root = Path(record.worktree)
            _, model_cfg = self._inquisitor(record)
            project = self.taskstore.get_project(record.project)
            tasks: ShapeTasks | None
            try:
                tasks = load_tasks(root)
            except ShapeError:
                tasks = None
            existing = None
            existing_text = None
            path = root / C4_DOC
            if path.is_file():
                existing_text = path.read_text(encoding="utf-8")
                try:
                    existing = load_c4(root)
                except ShapeError:
                    existing = None
            prompt = build_c4_prompt(
                project=project, root=root, tasks=tasks, existing=existing_text
            )
            pass_result = self._inquisitor_pass(
                record,
                prompt,
                model=model_cfg.model,
                session_id=record.session_id,
                append_log=True,
                prompt_name="c4.txt",
            )
            record.session_id = pass_result.session_id or record.session_id
            record.save(conn)
            doc = load_c4(root)
            if doc is None:
                raise ShapeError("the inquisitor did not write C4.yaml")
            if existing is not None and _c4_element_count(existing) and not _c4_element_count(doc):
                raise ShapeError(
                    "the update removed every C4 element; the file is left "
                    "uncommitted for you to fix"
                )
            self._commit(root, f"cc shape {record.id}: C4 from spec")
            check = check_shape(root)
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": record.project,
                    "state": record.state,
                },
            )
            return record
        except Exception as exc:
            record.state = "error"
            record.error = str(exc)
            record.save(conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": record.project,
                    "state": "error",
                },
            )
            raise
        finally:
            conn.close()

    def c4(self, project_id: str) -> dict[str, Any]:
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        root = Path(record.worktree)
        if not root.is_dir():
            raise ShapeError("the shape worktree is gone")
        doc = load_c4(root)
        return {
            "shape": record.id,
            "state": record.state,
            "c4": doc.model_dump(mode="json", by_alias=True) if doc else None,
            "editable": record.state in TREE_EDITABLE,
        }

    def save_c4(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        if record.state in {"drafting", "revising"}:
            raise ShapeError("the inquisitor is working; wait for it to finish")
        if record.state not in TREE_EDITABLE:
            raise ShapeError(f"shape is {record.state}; the C4 model is read-only")
        try:
            c4 = ShapeC4.model_validate(payload)
        except ValidationError as exc:
            raise ShapeError(_validation_text(exc)) from exc
        conn = self._conn()
        try:
            root = Path(record.worktree)
            if not root.is_dir():
                raise ShapeError("the shape worktree is gone")
            write_c4(root, c4)
            self._commit(root, f"cc shape {record.id}: edit C4 model")
            check = check_shape(root)
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": project_id,
                    "state": record.state,
                },
            )
            return {
                "shape": record.id,
                "state": record.state,
                "c4": c4.model_dump(mode="json", by_alias=True),
                "check": check.to_dict(),
                "editable": record.state in TREE_EDITABLE,
            }
        finally:
            conn.close()

    def use_cases(self, project_id: str) -> dict[str, Any]:
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        root = Path(record.worktree)
        if not root.is_dir():
            raise ShapeError("the shape worktree is gone")
        doc = load_use_cases(root)
        return {
            "shape": record.id,
            "state": record.state,
            "use_cases": (
                doc.model_dump(mode="json") if doc is not None else None
            ),
            "editable": record.state in TREE_EDITABLE,
        }

    def save_use_cases(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate a hand-edited workflow, write it, and re-check."""
        record = self._active_or_none(project_id)
        if record is None:
            raise ShapeError(f"no active shape for {project_id}")
        if record.state in {"drafting", "revising"}:
            raise ShapeError("the inquisitor is working; wait for it to finish")
        if record.state not in TREE_EDITABLE:
            raise ShapeError(
                f"shape is {record.state}; the workflows are read-only"
            )
        try:
            use_cases = ShapeUseCases.model_validate(payload)
        except ValidationError as exc:
            raise ShapeError(_validation_text(exc)) from exc
        conn = self._conn()
        try:
            root = Path(record.worktree)
            if not root.is_dir():
                raise ShapeError("the shape worktree is gone")
            tasks: ShapeTasks | None
            try:
                tasks = load_tasks(root)
            except ShapeError:
                tasks = None
            issues = use_case_task_issues(use_cases, tasks)
            if issues:
                raise ShapeError("; ".join(issues))
            write_use_cases(root, use_cases)
            self._commit(root, f"cc shape {record.id}: edit use cases")
            check = check_shape(root)
            self._finish_state(record, check, conn)
            self._publish(
                "shape.updated",
                {
                    "shape_id": record.id,
                    "project": project_id,
                    "state": record.state,
                },
            )
            return {
                "shape": record.id,
                "state": record.state,
                "use_cases": use_cases.model_dump(mode="json"),
                "check": check.to_dict(),
                "editable": record.state in TREE_EDITABLE,
            }
        finally:
            conn.close()

    def terminate(self) -> None:
        """Kill the turn in flight, if the agent can."""
        terminate = getattr(self.agent, "terminate", None)
        if callable(terminate):
            terminate()

    def discuss(
        self, project_id: str, message: str, question_id: str | None = None
    ) -> dict[str, Any]:
        """A read-only turn on the shape session: brainstorm, no file writes."""
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            if record.state not in TREE_EDITABLE:
                raise ShapeError(f"shape is {record.state}; it cannot be discussed")
            question_text = message.strip()
            if not question_text:
                raise ShapeError("say something first")
            _, model_cfg = self._inquisitor(record)
            model = model_cfg.model
            project = self.taskstore.get_project(project_id)
            try:
                questions = load_questions(Path(record.worktree))
            except ShapeError:
                questions = None
            prompt = build_discuss_prompt(
                project=project,
                record=record,
                questions=questions,
                message=question_text,
                question_id=question_id,
            )
            result = self._inquisitor_pass(
                record,
                prompt,
                model=model,
                session_id=record.session_id,
                append_log=True,
                prompt_name="discuss.txt",
                agent="cc-inquisitor-chat",
            )
            record.session_id = result.session_id or record.session_id
            record.save(conn)
            return {
                "reply": result.final_text,
                "session_id": record.session_id,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cost": result.cost,
                "context": result.context,
            }
        finally:
            conn.close()

    def _active_or_none(self, project_id: str) -> ShapeRecord | None:
        conn = self._conn()
        try:
            data = db.active_shape_for_project(conn, project_id)
            return ShapeRecord.model_validate(data) if data else None
        finally:
            conn.close()

    def check(self, project_id: str) -> tuple[ShapeRecord, ShapeCheck]:
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            if record.state in {"drafting", "revising"}:
                raise ShapeError(f"shape is {record.state}")
            check = check_shape(
                Path(record.worktree),
                require_materialized=(
                    record.state in {"materialized", "approving"}
                    or bool((record.check or {}).get("materialized"))
                ),
            )
            record.check = check.to_dict()
            if check.ok and check.materialized:
                record.state = "materialized"
                record.save(conn)
            else:
                self._finish_state(record, check, conn)
            return record, check
        finally:
            conn.close()

    def _materialize(self, record: ShapeRecord, conn) -> ShapeRecord:
        root = Path(record.worktree)
        project = self.taskstore.get_project(record.project)
        self._adopt_existing(record, root)
        tasks = load_tasks(root)
        for objective in tasks.objectives:
            existing = {item.id for item in project.objectives}
            if not objective.taskstore_id or objective.taskstore_id not in existing:
                created = self.taskstore.create_objective(
                    project.id,
                    {
                        "title": objective.title,
                        "description": objective.detail,
                    },
                )
                objective.taskstore_id = created.id
                write_tasks(root, tasks)
                project = self.taskstore.get_project(record.project)
            for task in objective.tasks:
                objective_state = next(
                    item
                    for item in project.objectives
                    if item.id == objective.taskstore_id
                )
                existing_tasks = {item.id for item in objective_state.tasks}
                if task.taskstore_id and task.taskstore_id in existing_tasks:
                    continue
                created = self.taskstore.create_task(
                    project.id,
                    objective.taskstore_id,
                    {
                        "title": task.title,
                        "note": task.detail,
                    },
                )
                task.taskstore_id = created.id
                write_tasks(root, tasks)
                project = self.taskstore.get_project(record.project)
        self._write_gate(root, tasks)
        self._commit(root, f"cc shape {record.id}: materialize")
        check = check_shape(root, require_materialized=True)
        record.check = check.to_dict()
        record.state = "materialized" if check.ok else "check_failed"
        record.save(conn)
        if not check.ok:
            raise ShapeError("; ".join(check.issues))
        return record

    def materialize(self, project_id: str) -> ShapeRecord:
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            if record.state in {"drafting", "revising", "approved", "cancelled"}:
                raise ShapeError(f"shape is {record.state}")
            precheck = check_shape(Path(record.worktree))
            if not precheck.ok:
                record.check = precheck.to_dict()
                record.state = "check_failed"
                record.save(conn)
                raise ShapeError("; ".join(precheck.issues))
            return self._materialize(record, conn)
        finally:
            conn.close()

    def _write_gate(self, root: Path, tasks: ShapeTasks) -> None:
        task_map = {
            task.slug: task.taskstore_id
            for objective in tasks.objectives
            for task in objective.tasks
            if task.taskstore_id
        }
        plan: dict[str, Any] = {
            "version": 1,
            "defaults": {
                "max_attempts": self.config.budgets.max_attempts,
                "gate": [],
            },
            "objectives": {},
            "tasks": {},
        }
        for objective in tasks.objectives:
            if not objective.taskstore_id:
                raise ShapeError(f"objective {objective.slug!r} has no taskstore_id")
            plan["objectives"][objective.taskstore_id] = {
                "gate": objective.gate,
            }
            for task in objective.tasks:
                if not task.taskstore_id:
                    raise ShapeError(f"task {task.slug!r} has no taskstore_id")
                plan["tasks"][task.taskstore_id] = {
                    "gate": task.gate,
                    "depends_on": [
                        task_map[dependency]
                        for dependency in task.depends_on
                        if dependency in task_map
                    ],
                    "scope": task.scope,
                }
        _yaml_write(root / GATE_DOC, plan)

    def _verify_taskstore_ids(self, project: TaskStoreProject, tasks: ShapeTasks) -> None:
        objectives = {objective.id: objective for objective in project.objectives}
        for objective in tasks.objectives:
            if not objective.taskstore_id or objective.taskstore_id not in objectives:
                raise ShapeError(
                    f"objective {objective.slug!r} is not materialized in TaskStore"
                )
            current = objectives[objective.taskstore_id]
            task_ids = {task.id for task in current.tasks}
            for task in objective.tasks:
                if not task.taskstore_id or task.taskstore_id not in task_ids:
                    raise ShapeError(
                        f"task {task.slug!r} is not materialized in TaskStore"
                    )

    def approve(self, project_id: str) -> ShapeRecord:
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            if record.state in {"drafting", "revising", "cancelled"}:
                raise ShapeError(f"shape is {record.state}")
            if record.state == "error" and not Path(record.worktree).is_dir():
                raise ShapeError("shape is in error; cancel it first")
            if record.state != "materialized":
                record = self.materialize(project_id)
            check = check_shape(Path(record.worktree), require_materialized=True)
            record.check = check.to_dict()
            if not check.ok:
                record.state = "check_failed"
                record.save(conn)
                raise ShapeError("; ".join(check.issues))
            project = self.taskstore.get_project(project_id)
            self._verify_taskstore_ids(project, load_tasks(Path(record.worktree)))
            root = project_root(project.path)
            if root is None:
                raise ShapeError("project has no checkout")
            if not gitutil.is_clean(Path(record.worktree)):
                raise ShapeError("shape worktree has uncommitted changes")
            shape_head = gitutil.head_commit(Path(record.worktree), "HEAD")
            current = (
                gitutil.head_commit(root, "cc/integration")
                if gitutil.branch_exists(root, "cc/integration")
                else ""
            )
            if current not in {record.base_sha, shape_head}:
                raise ShapeError("cc/integration moved since the shape started")
            if current != shape_head:
                gitutil.move_branch(root, "cc/integration", shape_head)
            docs = spec.default_docs(Path(record.worktree))
            digest = spec.compute_digest(root, docs, "cc/integration")
            approved_at = _utcnow().replace(microsecond=0)
            state = {
                "docs": docs,
                "digest": digest,
                "approved_at": approved_at.isoformat().replace("+00:00", "Z"),
                "approved_in_commit": shape_head,
                "dirty": False,
                "phase": "approved",
            }
            try:
                self._patch_spec_state(project_id, state)
            except Exception as exc:
                record.state = "error"
                record.error = f"integration merged; spec_state patch failed: {exc}"
                record.approved_commit = shape_head
                record.save(conn)
                raise
            gitutil.worktree_remove(root, Path(record.worktree))
            record.state = "approved"
            record.approved_commit = shape_head
            record.error = None
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": project_id, "state": "approved"},
            )
            return record
        finally:
            conn.close()

    def cancel(self, project_id: str) -> ShapeRecord:
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            project = self.taskstore.get_project(project_id)
            root = project_root(project.path)
            if root is not None and Path(record.worktree).exists():
                gitutil.worktree_remove(root, Path(record.worktree))
            previous = record.previous_spec_state
            if previous is not None:
                self._patch_spec_state(project_id, previous)
            else:
                current = (
                    project.spec_state.model_dump(mode="json")
                    if project.spec_state
                    else {"docs": [], "digest": "", "dirty": False, "phase": "shape"}
                )
                current["phase"] = "shape"
                self._patch_spec_state(project_id, current)
            record.state = "cancelled"
            record.save(conn)
            self._publish(
                "shape.updated",
                {"shape_id": record.id, "project": project_id, "state": "cancelled"},
            )
            return record
        finally:
            conn.close()

    def status(self, project_id: str, *, include_latest: bool = False) -> dict[str, Any]:
        conn = self._conn()
        try:
            data = (
                db.latest_shape(conn, project_id)
                if include_latest
                else db.active_shape_for_project(conn, project_id)
            )
            if data is None:
                return {"shape": None}
            record = ShapeRecord.model_validate(data)
            questions = (
                _shape_questions(Path(record.worktree))
                if Path(record.worktree).is_dir()
                else None
            )
            return {
                "shape": record.model_dump(mode="json"),
                "questions": (
                    questions.model_dump(mode="json") if questions else None
                ),
            }
        finally:
            conn.close()

    def diff(self, project_id: str) -> str:
        conn = self._conn()
        try:
            record = self._active(conn, project_id)
            project = self.taskstore.get_project(project_id)
            root = project_root(project.path)
            if root is None:
                raise ShapeError("project has no checkout")
            return gitutil.diff_patch(root, record.base_sha, record.branch)
        except gitutil.GitError as exc:
            raise ShapeError(str(exc)) from exc
        finally:
            conn.close()
