from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from command_center.cockpit.theme import apply_theme  # noqa: E402
from command_center.cockpit.widgets import (  # noqa: E402
    DiffView,
    RunsWidget,
    SettingsWidget,
    make_icon,
)


class FakeApi:
    def settings(self):
        return {
            "version": "p4",
            "daemon": {"bind": "127.0.0.1", "port": 4190},
            "taskstore": {"url": "http://127.0.0.1:4180"},
            "paths": {
                "state_dir": "/tmp/state",
                "worktree_root": "/tmp/work",
                "token_path": "/tmp/token",
                "token_exists": False,
            },
            "opencode": {"binary": "opencode", "agents_dir": "/tmp/agents"},
            "budgets": {"max_attempts": 3, "gate_timeout_seconds": 900},
            "policy": {"max_files": 200, "max_diff_bytes": 1000000},
            "roles": {"coder": "hosted", "reviewer": "local"},
            "autonomy": "fixed",
        }

    def models(self, force: bool = False):
        return {
            "models": [
                {
                    "id": "hosted",
                    "label": "Hosted model",
                    "model": "provider/model-name",
                    "up": True,
                    "reason": "key",
                }
            ]
        }


def test_diff_view_builds() -> None:
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    view = DiffView()
    view.set_patch(
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert view.files_list.count() == 1
    assert view.table.rowCount() >= 2
    assert not make_icon().isNull()


def test_settings_widget_is_read_only() -> None:
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    widget = SettingsWidget(FakeApi())
    assert widget.findChildren(QLineEdit) == []
    assert widget.isEnabled()


def test_live_log_formatter() -> None:
    assert (
        RunsWidget._format_log_line('{"type":"step_start"}')
        == "── step start ──"
    )
    assert (
        RunsWidget._format_log_line(
            '{"part":{"type":"text","text":"hello"}}'
        )
        == "assistant: hello"
    )
    rendered = RunsWidget._format_log_line(
        '{"part":{"type":"tool","tool":"shell","state":{"input":'
        '{"command":"pwd"},"output":"/tmp"}}}'
    )
    assert "tool shell: pwd" in rendered
    assert "/tmp" in rendered
