from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeTaskStore, commit, git, make_project

from command_center import spec
from command_center.errors import SpecError


def test_approve_and_check(repo: Path) -> None:
    project = make_project(project_id="p1", path=str(repo))
    taskstore = FakeTaskStore([project])
    result = spec.approve(taskstore, "p1", ["SPEC.md"])
    assert result.docs == ["SPEC.md"]
    assert result.digest.startswith("sha256:")
    assert result.commit == git(repo, "rev-parse", "HEAD").stdout.strip()
    approved = taskstore.projects["p1"]
    assert approved.spec_state is not None
    assert approved.spec_state.phase == "approved"
    assert approved.spec_state.digest == result.digest
    assert spec.check(approved).ok


def test_approve_refuses_dirty_and_untracked(repo: Path) -> None:
    project = make_project(project_id="p1", path=str(repo))
    taskstore = FakeTaskStore([project])
    (repo / "SPEC.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SpecError):
        spec.approve(taskstore, "p1", ["SPEC.md"])
    git(repo, "checkout", "--", "SPEC.md")
    (repo / "NEW.md").write_text("new\n", encoding="utf-8")
    with pytest.raises(SpecError):
        spec.approve(taskstore, "p1", ["NEW.md"])
    with pytest.raises(SpecError):
        spec.approve(taskstore, "p1", ["MISSING.md"])


def test_check_detects_changed_docs(repo: Path) -> None:
    project = make_project(project_id="p1", path=str(repo))
    taskstore = FakeTaskStore([project])
    spec.approve(taskstore, "p1", ["SPEC.md"])
    (repo / "SPEC.md").write_text("changed\n", encoding="utf-8")
    commit(repo, "change spec")
    result = spec.check(taskstore.projects["p1"])
    assert not result.ok
    assert result.code == "spec_dirty"


def test_check_requires_approval(repo: Path) -> None:
    project = make_project(project_id="p1", path=str(repo))
    result = spec.check(project)
    assert not result.ok
    assert result.code == "spec_unapproved"
