from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeTaskStore, make_project

from command_center import db
from command_center.errors import FocusError
from command_center.focus import FocusService


def service(tmp_path: Path, projects):
    conn = db.connect(tmp_path / "state")
    taskstore = FakeTaskStore(projects)
    return conn, taskstore, FocusService(conn, taskstore)


def test_imports_existing_tags(tmp_path: Path) -> None:
    projects = [
        make_project(project_id=f"p{index}", focus_order=index)
        for index in (1, 2, 3)
    ]
    _, taskstore, focus = service(tmp_path, projects)
    state = focus.list()
    assert [entry.project_id for entry in state.entries] == ["p1", "p2", "p3"]
    assert [entry.position for entry in state.entries] == [1, 2, 3]
    assert taskstore.project_calls == []


def test_set_replaces_and_clears_tags(tmp_path: Path) -> None:
    projects = [
        make_project(project_id=f"p{index}", focus_order=index)
        for index in (1, 2, 3)
    ]
    _, taskstore, focus = service(tmp_path, projects)
    state = focus.set(["p3", "p1"])
    assert [entry.project_id for entry in state.entries] == ["p3", "p1"]
    assert taskstore.projects["p3"].focus_order == 1
    assert taskstore.projects["p1"].focus_order == 2
    assert taskstore.projects["p2"].focus_order is None
    assert state.tag_errors == []


def test_set_rejects_invalid(tmp_path: Path) -> None:
    projects = [make_project(project_id=f"p{index}") for index in (1, 2, 3, 4)]
    _, _, focus = service(tmp_path, projects)
    with pytest.raises(FocusError):
        focus.set(["p1", "p2", "p3", "p4"])
    with pytest.raises(FocusError):
        focus.set(["p1", "p1"])
    with pytest.raises(FocusError):
        focus.set(["missing"])
    projects[0] = make_project(project_id="p1", status="archived")
    _, _, focus = service(tmp_path, projects)
    with pytest.raises(FocusError):
        focus.set(["p1"])


def test_archived_project_is_removed(tmp_path: Path) -> None:
    projects = [
        make_project(project_id="p1", focus_order=1),
        make_project(project_id="p2", status="archived", focus_order=2),
    ]
    conn, taskstore, focus = service(tmp_path, projects)
    db.set_focus(conn, ["p1", "p2"])
    state = focus.list()
    assert [entry.project_id for entry in state.entries] == ["p1"]
    assert taskstore.projects["p2"].focus_order is None


def test_stale_focus(tmp_path: Path) -> None:
    projects = [make_project(project_id="p1", focus_order=1)]
    conn, _, focus = service(tmp_path, projects)
    db.set_focus(conn, ["p1"], "2020-01-01T00:00:00Z")
    assert focus.list().stale is True
