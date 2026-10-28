from __future__ import annotations

from pathlib import Path
from typing import Annotated, TypeAlias

from pydantic import BaseModel, Field, ValidationError, field_validator
from ruamel.yaml import YAML

from .errors import PlanError

GateCommand: TypeAlias = Annotated[list[str], Field(min_length=1)]
Gate: TypeAlias = list[GateCommand]


class PlanTask(BaseModel):
    gate: Gate = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)

    @field_validator("scope")
    @classmethod
    def _scope_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("scope entries cannot be empty")
        return value


class PlanObjective(BaseModel):
    gate: Gate = Field(default_factory=list)


class PlanProject(BaseModel):
    gate: Gate = Field(default_factory=list)


class PlanDefaults(BaseModel):
    gate: Gate = Field(default_factory=list)
    max_attempts: int = Field(default=3, ge=1)


class Plan(BaseModel):
    version: int = 1
    defaults: PlanDefaults = Field(default_factory=PlanDefaults)
    project: PlanProject = Field(default_factory=PlanProject)
    objectives: dict[str, PlanObjective] = Field(default_factory=dict)
    tasks: dict[str, PlanTask] = Field(default_factory=dict)

    def gate_for(self, objective_id: str, task_id: str) -> Gate | None:
        task = self.tasks.get(task_id)
        if task and task.gate:
            return task.gate
        objective = self.objectives.get(objective_id)
        if objective and objective.gate:
            return objective.gate
        if self.project.gate:
            return self.project.gate
        if self.defaults.gate:
            return self.defaults.gate
        return None

    def deps_for(self, task_id: str) -> list[str]:
        task = self.tasks.get(task_id)
        return list(task.depends_on) if task else []

    def scope_for(self, task_id: str) -> list[str]:
        task = self.tasks.get(task_id)
        return list(task.scope) if task else []

    def has_gate(self, objective_id: str, task_id: str) -> bool:
        return self.gate_for(objective_id, task_id) is not None


def _parse_plan(data: object, label: str) -> Plan:
    if not isinstance(data, dict):
        raise PlanError(f"{label}: expected a YAML mapping")
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"{label}: {exc}") from exc
    if plan.version != 1:
        raise PlanError(f"{label}: unsupported version {plan.version}")
    return plan


def load_plan(root: Path) -> Plan:
    path = Path(root) / "command-center.yaml"
    if not path.is_file():
        raise PlanError(f"{path}: no command-center.yaml")
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise PlanError(f"{path}: {exc}") from exc
    return _parse_plan(data, str(path))


def load_plan_ref(root: Path, ref: str) -> Plan:
    from . import gitutil

    path = f"{ref}:command-center.yaml"
    try:
        content = gitutil.blob(root, ref, "command-center.yaml")
    except gitutil.GitError as exc:
        raise PlanError(f"{path}: {exc}") from exc
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(content.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise PlanError(f"{path}: {exc}") from exc
    return _parse_plan(data, path)
