from __future__ import annotations

import json
from datetime import date
from typing import Any

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..client import DaemonClient, DaemonError
from .diff import parse_patch, side_rows
from .theme import BAD, BG2, CHROME, GOOD, INFO, MUT, SURFACE2, WARN

MONO = "DejaVu Sans Mono"
TERMINAL_STATES = {"merged", "blocked", "error", "cancelled"}


def error_box(parent: QWidget, exc: Exception) -> None:
    payload = getattr(exc, "payload", None) or {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    text = detail if isinstance(detail, str) and detail else str(exc)
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if issues:
        text += "\n\n" + "\n".join(
            f"{issue.get('item_id') or 'agenda'}: {issue.get('code')} — {issue.get('detail', '')}"
            for issue in issues
        )
    QMessageBox.critical(parent, "Command Center", text)


def table_item(text: Any, *, mono: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text if text is not None else ""))
    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
    if mono:
        item.setFont(QFont(MONO, 9))
    return item


def status_colour(state: str) -> QColor:
    if state in {"merged", "done", "approve", "shipped", "live"}:
        return QColor(GOOD)
    if state in {
        "blocked",
        "error",
        "cancelled",
        "rejected",
        "block",
        "policy_failed",
        "gate_failed",
        "no_diff",
        "review_block",
        "review_changes",
    }:
        return QColor(BAD)
    if state in {"running", "queued", "review", "review_changes", "merging", "doing"}:
        return QColor(WARN)
    if state in {"info"}:
        return QColor(INFO)
    return QColor(MUT)


def make_icon(size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(BG2))
    painter.setPen(QPen(QColor(CHROME), max(2, size // 24)))
    margin = size // 8
    painter.drawRoundedRect(
        margin,
        margin,
        size - margin * 2,
        size - margin * 2,
        size // 8,
        size // 8,
    )
    painter.setPen(QPen(QColor(CHROME), max(2, size // 24)))
    cx, cy = size / 2, size / 2
    radius = size * 0.23
    points = []
    for index in range(6):
        import math

        angle = math.radians(60 * index - 30)
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    for index in range(6):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % 6]
        painter.drawLine(int(x1), int(y1), int(x2), int(y2))
    painter.setPen(QPen(QColor(CHROME), max(1, size // 40)))
    painter.drawLine(int(cx), int(cy - radius * 0.5), int(cx), int(cy + radius * 0.5))
    painter.end()
    return QIcon(pixmap)


class DiffView(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.files = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.files_list = QListWidget()
        self.files_list.setMinimumWidth(220)
        self.files_list.setMaximumWidth(360)
        self.files_list.currentRowChanged.connect(self._show_file)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["BEFORE", "AFTER"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        splitter.addWidget(self.files_list)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    def set_patch(self, text: str) -> None:
        self.files = parse_patch(text)
        self.files_list.blockSignals(True)
        self.files_list.clear()
        for file in self.files:
            label = f"{file.display_name}  +{file.additions} −{file.deletions}"
            if file.binary:
                label = f"{file.display_name}  [binary]"
            self.files_list.addItem(QListWidgetItem(label))
        self.files_list.blockSignals(False)
        if self.files:
            self.files_list.setCurrentRow(0)
            self._show_file(0)
        else:
            self.table.setRowCount(0)

    def _show_file(self, index: int) -> None:
        if index < 0 or index >= len(self.files):
            self.table.setRowCount(0)
            return
        file = self.files[index]
        rows = []
        for hunk in file.hunks:
            rows.extend(side_rows(hunk))
        self.table.clearSpans()
        self.table.setRowCount(min(len(rows), 5000))
        for row_index, row in enumerate(rows[:5000]):
            if row.kind == "hunk":
                self.table.setSpan(row_index, 0, 1, 2)
                item = table_item(row.left, mono=True)
                item.setForeground(QColor(CHROME))
                item.setBackground(QColor(SURFACE2))
                self.table.setItem(row_index, 0, item)
                continue
            left = f"{row.left_no:>4}  {row.left}" if row.left_no is not None else ""
            right = f"{row.right_no:>4}  {row.right}" if row.right_no is not None else ""
            left_item = table_item(left, mono=True)
            right_item = table_item(right, mono=True)
            if row.kind == "replace":
                left_item.setBackground(QColor("#2a1518"))
                right_item.setBackground(QColor("#11251d"))
            elif row.kind == "del":
                left_item.setBackground(QColor("#2a1518"))
            elif row.kind == "add":
                right_item.setBackground(QColor("#11251d"))
            self.table.setItem(row_index, 0, left_item)
            self.table.setItem(row_index, 1, right_item)
        if len(rows) > 5000:
            row_index = 5000
            self.table.insertRow(row_index)
            self.table.setRowCount(5001)
            self.table.setSpan(row_index, 0, 1, 2)
            self.table.setItem(
                row_index,
                0,
                table_item(f"… {len(rows) - 5000} more diff rows hidden", mono=True),
            )


class QuickAddDialog(QDialog):
    def __init__(self, projects: list[dict[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Quick add")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.project = QComboBox()
        for project in projects:
            self.project.addItem(project["name"], project["id"])
        self.title = QLineEdit()
        self.title.setPlaceholderText("What needs doing?")
        self.due = QLineEdit()
        self.due.setPlaceholderText("HH:MM (optional, manual item)")
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Why today? (optional)")
        form.addRow("Project", self.project)
        form.addRow("Title", self.title)
        form.addRow("Due", self.due)
        form.addRow("Reason", self.reason)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class TaskPickDialog(QDialog):
    def __init__(self, tasks: list[dict[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add existing task")
        self.setMinimumWidth(620)
        self.tasks = tasks
        layout = QVBoxLayout(self)
        self.combo = QComboBox()
        for task in tasks:
            label = f"{task['project']} / {task['title']} ({task['task']})"
            self.combo.addItem(label, task)
        self.kind = QComboBox()
        self.kind.addItem("work", "work")
        self.kind.addItem("manual", "manual")
        layout.addWidget(self.combo)
        layout.addWidget(self.kind)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> dict[str, Any] | None:
        value = self.combo.currentData()
        return value if isinstance(value, dict) else None

    def selected_kind(self) -> str:
        return str(self.kind.currentData())


class AgendaWidget(QWidget):
    changed = Signal()

    def __init__(self, api: DaemonClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.api = api
        self._agenda: dict[str, Any] | None = None
        self._exists = False
        self._projects: list[dict[str, Any]] = []
        self._focus: dict[str, Any] = {"entries": []}
        self._tasks: list[dict[str, Any]] = []
        self._focus_checks: dict[str, QCheckBox] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.dateChanged.connect(self._load_agenda)
        top.addWidget(QLabel("Day"))
        top.addWidget(self.date_edit)
        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self.refresh)
        top.addWidget(self.load_button)
        self.suggest_button = QPushButton("Suggest")
        self.suggest_button.clicked.connect(self._suggest)
        top.addWidget(self.suggest_button)
        self.lock_button = QPushButton("Lock")
        self.lock_button.clicked.connect(self._lock)
        top.addWidget(self.lock_button)
        self.unlock_button = QPushButton("Unlock")
        self.unlock_button.setObjectName("ghost")
        self.unlock_button.clicked.connect(self._unlock)
        top.addWidget(self.unlock_button)
        top.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("ghost")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.meta = QLabel("loading…")
        self.meta.setObjectName("muted")
        layout.addWidget(self.meta)

        focus_box = QGroupBox("Focus")
        focus_layout = QVBoxLayout(focus_box)
        focus_header = QHBoxLayout()
        focus_header.addWidget(QLabel("Tags drive suggestions; max three."))
        focus_header.addStretch(1)
        save_focus = QPushButton("Save focus")
        save_focus.setObjectName("ghost")
        save_focus.clicked.connect(self._save_focus)
        focus_header.addWidget(save_focus)
        focus_layout.addLayout(focus_header)
        self.focus_scroll = QScrollArea()
        self.focus_scroll.setWidgetResizable(True)
        self.focus_scroll.setMaximumHeight(150)
        self.focus_container = QWidget()
        self.focus_columns = QGridLayout(self.focus_container)
        self.focus_columns.setContentsMargins(2, 2, 2, 2)
        self.focus_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.focus_scroll.setWidget(self.focus_container)
        focus_layout.addWidget(self.focus_scroll)
        layout.addWidget(focus_box)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Add existing task")
        self.add_button.setObjectName("ghost")
        self.add_button.clicked.connect(self._add_existing)
        actions.addWidget(self.add_button)
        self.quick_button = QPushButton("Quick add")
        self.quick_button.clicked.connect(self._quick_add)
        actions.addWidget(self.quick_button)
        actions.addStretch(1)
        self.up_button = QPushButton("↑")
        self.up_button.setObjectName("ghost")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("↓")
        self.down_button.setObjectName("ghost")
        self.down_button.clicked.connect(lambda: self._move(1))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("ghost")
        self.remove_button.clicked.connect(self._remove)
        actions.addWidget(self.up_button)
        actions.addWidget(self.down_button)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Kind", "Project / task", "Reason", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(
            3, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._reason_changed)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.save_button = QPushButton("Save agenda")
        self.save_button.clicked.connect(self._save_agenda)
        bottom.addStretch(1)
        bottom.addWidget(self.save_button)
        layout.addLayout(bottom)
        self.message = QLabel("")
        self.message.setObjectName("muted")
        layout.addWidget(self.message)

    def day(self) -> date:
        return self.date_edit.date().toPython()

    def refresh(self) -> None:
        try:
            projects = self.api.projects()["projects"]
            self._projects = [
                project for project in projects if project.get("status") != "archived"
            ]
            self._focus = self.api.focus()
            self._tasks = self.api.tasks()["tasks"]
        except DaemonError as exc:
            error_box(self, exc)
            return
        self._load_agenda()

    def _load_agenda(self) -> None:
        try:
            agenda = self.api.agenda(self.day())
        except DaemonError as exc:
            error_box(self, exc)
            return
        self._exists = agenda is not None
        self._agenda = agenda or {
            "date": self.day().isoformat(),
            "status": "draft",
            "rev": 0,
            "focus": [],
            "capacity": {"work_items": 3, "chores": 3, "operator_min": 90},
            "items": [],
        }
        self._render()

    def _render(self) -> None:
        agenda = self._agenda
        if agenda is None:
            return
        locked = agenda.get("status") == "locked"
        focus_text = ", ".join(agenda.get("focus") or []) or "—"
        self.meta.setText(
            f"{agenda.get('status', 'draft').upper()}   rev {agenda.get('rev', 0)}"
            f"   focus {focus_text}"
        )
        self.save_button.setEnabled(not locked)
        self.lock_button.setEnabled(not locked)
        self.unlock_button.setEnabled(locked)
        self.add_button.setEnabled(not locked)
        self.quick_button.setEnabled(not locked)
        self.up_button.setEnabled(not locked)
        self.down_button.setEnabled(not locked)
        self.remove_button.setEnabled(not locked)
        self.message.setText(
            "Locked: content is immutable. Unlock to edit."
            if locked
            else f"{len(agenda.get('items', []))} item(s); work cap "
            f"{agenda.get('capacity', {}).get('work_items', 3)}."
        )
        self._render_focus()
        self._render_items()

    def _render_focus(self) -> None:
        while self.focus_columns.count():
            child = self.focus_columns.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
        self._focus_checks = {}
        checked = {entry.get("project") for entry in self._focus.get("entries", [])}
        for index, project in enumerate(self._projects):
            box = QCheckBox(project["name"])
            box.setChecked(project["id"] in checked)
            box.setToolTip(project["id"])
            self._focus_checks[project["id"]] = box
            self.focus_columns.addWidget(box, index // 2, index % 2)

    def _render_items(self) -> None:
        agenda = self._agenda
        if agenda is None:
            return
        locked = agenda.get("status") == "locked"
        self.table.blockSignals(True)
        items = agenda.get("items", [])
        self.table.setRowCount(len(items))
        for index, item in enumerate(items):
            self.table.setItem(index, 0, table_item(index + 1))
            self.table.setItem(index, 1, table_item(item.get("kind", "")))
            self.table.setItem(
                index,
                2,
                table_item(
                    f"{item.get('project', '')} / {item.get('task', '')}",
                    mono=True,
                ),
            )
            reason = QTableWidgetItem(item.get("reason", ""))
            if not locked:
                reason.setFlags(
                    reason.flags()
                    | Qt.ItemFlag.ItemIsEditable
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsEnabled
                )
            self.table.setItem(index, 3, reason)
            status = table_item(item.get("status", "planned"))
            status.setForeground(status_colour(item.get("status", "planned")))
            self.table.setItem(index, 4, status)
        self.table.blockSignals(False)

    def _reason_changed(self, item: QTableWidgetItem) -> None:
        if self._agenda is None or item.column() != 3:
            return
        index = item.row()
        items = self._agenda.get("items", [])
        if 0 <= index < len(items):
            items[index]["reason"] = item.text()

    def _selected(self) -> int:
        return self.table.currentRow()

    def _add_existing(self) -> None:
        tasks = [task for task in self._tasks if task.get("status") in {"open", "doing"}]
        if not tasks:
            QMessageBox.information(self, "Command Center", "No open tasks.")
            return
        dialog = TaskPickDialog(tasks, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        task = dialog.selected()
        if task is None or self._agenda is None:
            return
        self._agenda.setdefault("items", []).append(
            {
                "kind": dialog.selected_kind(),
                "project": task["project"],
                "objective": task["objective"],
                "task": task["task"],
                "reason": "",
                "status": "planned",
                "run": None,
            }
        )
        self._render_items()

    def _quick_add(self) -> None:
        if self._agenda is None or self._agenda.get("status") == "locked":
            return
        dialog = QuickAddDialog(self._projects, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title = dialog.title.text().strip()
        if not title:
            QMessageBox.warning(self, "Command Center", "Title is required.")
            return
        project = dialog.project.currentData()
        try:
            result = self.api.quick_add(
                self.day(),
                {
                    "project": project,
                    "title": title,
                    "due_time": dialog.due.text().strip() or None,
                    "reason": dialog.reason.text().strip(),
                    "if_rev": self._agenda.get("rev", 0),
                },
            )
            self._agenda = result["agenda"]
            self._exists = True
            self._tasks = self.api.tasks()["tasks"]
            self._render()
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)

    def _move(self, direction: int) -> None:
        if self._agenda is None or self._agenda.get("status") == "locked":
            return
        index = self._selected()
        items = self._agenda.get("items", [])
        target = index + direction
        if index < 0 or target < 0 or target >= len(items):
            return
        items[index], items[target] = items[target], items[index]
        self._render_items()
        self.table.selectRow(target)

    def _remove(self) -> None:
        if self._agenda is None or self._agenda.get("status") == "locked":
            return
        index = self._selected()
        items = self._agenda.get("items", [])
        if 0 <= index < len(items):
            items.pop(index)
            self._render_items()

    def _save_focus(self) -> None:
        selected = [project_id for project_id, box in self._focus_checks.items() if box.isChecked()]
        if len(selected) > 3:
            QMessageBox.warning(self, "Command Center", "Focus allows at most three projects.")
            return
        current = {entry["project"]: entry["position"] for entry in self._focus.get("entries", [])}
        selected.sort(key=lambda project_id: current.get(project_id, 99))
        try:
            self._focus = self.api.set_focus(selected)
            self._render_focus()
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)

    def _save_agenda(self) -> None:
        if self._agenda is None or self._agenda.get("status") == "locked":
            return
        focus = self._agenda.get("focus") or [
            entry["project"] for entry in self._focus.get("entries", [])[:3]
        ]
        items = []
        for index, item in enumerate(self._agenda.get("items", [])):
            items.append(
                {
                    "id": item.get("id") or None,
                    "kind": item.get("kind", "work"),
                    "project": item["project"],
                    "objective": item["objective"],
                    "task": item["task"],
                    "reason": item.get("reason", ""),
                    "estimate_min": item.get("estimate_min"),
                    "due_time": item.get("due_time"),
                    "order": index,
                    "note": item.get("note"),
                }
            )
        body = {
            "focus": focus[:3],
            "capacity": self._agenda.get(
                "capacity", {"work_items": 3, "chores": 3, "operator_min": 90}
            ),
            "items": items,
            "generated_by": "manual",
            "if_rev": self._agenda.get("rev", 0),
        }
        try:
            self._agenda = self.api.put_agenda(self.day(), body)
            self._exists = True
            self._render()
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)

    def _suggest(self) -> None:
        replace = self._exists
        if replace:
            answer = QMessageBox.question(
                self,
                "Command Center",
                "Replace the current draft with a new suggestion?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self._agenda = self.api.suggest_agenda(self.day(), replace=replace)
            self._exists = True
            self._render()
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)

    def _lock(self) -> None:
        if self._agenda is None:
            return
        try:
            result = self.api.lock_agenda(self.day(), if_rev=self._agenda.get("rev", 0))
            self._agenda = result["agenda"]
            self._exists = True
            self._render()
            warnings = result.get("warnings", [])
            if warnings:
                self.message.setText(
                    "Locked with warnings: "
                    + ", ".join(warning.get("code", "") for warning in warnings)
                )
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)

    def _unlock(self) -> None:
        reason, ok = QInputDialog.getText(self, "Unlock agenda", "Reason:")
        if not ok or not reason.strip():
            return
        try:
            self._agenda = self.api.unlock_agenda(self.day(), reason.strip())
            self._exists = True
            self._render()
            self.changed.emit()
        except DaemonError as exc:
            error_box(self, exc)


class QueueWidget(QWidget):
    run_started = Signal(str)

    def __init__(self, api: DaemonClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.api = api
        self._tasks: list[dict[str, Any]] = []
        self._build()
        self._load_models()
        self.refresh()

    def _load_models(self) -> None:
        try:
            models = self.api.models()["models"]
        except DaemonError as exc:
            error_box(self, exc)
            return
        self.model.clear()
        self.reviewer.clear()
        for model in models:
            if model.get("up"):
                self.model.addItem(model["label"], model["id"])
                self.reviewer.addItem(model["label"], model["id"])
        self.model.setEnabled(self.model.count() > 0)
        self.reviewer.setEnabled(self.reviewer.count() > 0)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.project = QComboBox()
        self.project.currentIndexChanged.connect(self._render)
        self.model = QComboBox()
        self.reviewer = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("ghost")
        refresh.clicked.connect(self.refresh)
        top.addWidget(QLabel("Project"))
        top.addWidget(self.project)
        top.addWidget(QLabel("Build"))
        top.addWidget(self.model)
        top.addWidget(QLabel("Reviewer"))
        top.addWidget(self.reviewer)
        top.addStretch(1)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Project", "Task", "Title", "State", "Reasons", "Ready"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(lambda _index: self._run())
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.message = QLabel("")
        self.message.setObjectName("muted")
        bottom.addWidget(self.message, 1)
        self.run_button = QPushButton("Run selected task")
        self.run_button.clicked.connect(self._run)
        self.run_button.setEnabled(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        bottom.addWidget(self.run_button)
        layout.addLayout(bottom)

    def refresh(self) -> None:
        try:
            projects = self.api.projects()["projects"]
            tasks = self.api.tasks()["tasks"]
        except DaemonError as exc:
            error_box(self, exc)
            return
        archived = {project["id"] for project in projects if project.get("status") == "archived"}
        self._tasks = [task for task in tasks if task.get("project") not in archived]
        current_project = self.project.currentData()
        self.project.blockSignals(True)
        self.project.clear()
        self.project.addItem("all projects", None)
        projects = sorted({task["project"] for task in tasks})
        for project in projects:
            self.project.addItem(project, project)
        index = self.project.findData(current_project)
        self.project.setCurrentIndex(max(index, 0))
        self.project.blockSignals(False)
        self._render()

    def _render(self) -> None:
        project = self.project.currentData()
        tasks = [task for task in self._tasks if not project or task["project"] == project]
        self.table.setRowCount(len(tasks))
        for index, task in enumerate(tasks):
            self.table.setItem(index, 0, table_item(task["project"], mono=True))
            self.table.setItem(index, 1, table_item(task["task"], mono=True))
            self.table.setItem(index, 2, table_item(task["title"]))
            state = table_item(task.get("status", ""))
            state.setForeground(status_colour(task.get("status", "")))
            self.table.setItem(index, 3, state)
            reasons = ", ".join(item["code"] for item in task.get("reasons", []))
            warnings = ", ".join(item["code"] for item in task.get("warnings", []))
            text = reasons or "ready"
            if warnings:
                text += f"  (warn {warnings})"
            self.table.setItem(index, 4, table_item(text))
            ready = table_item("yes" if task.get("ready") else "no")
            ready.setForeground(QColor(GOOD if task.get("ready") else MUT))
            self.table.setItem(index, 5, ready)
            self.table.item(index, 1).setData(Qt.ItemDataRole.UserRole, task)
        self.message.setText(f"{len(tasks)} task(s)")

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        ready = False
        if row >= 0:
            item = self.table.item(row, 1)
            if item is not None:
                task = item.data(Qt.ItemDataRole.UserRole)
                ready = bool(isinstance(task, dict) and task.get("ready"))
        self.run_button.setEnabled(ready and self.model.count() > 0)

    def _run(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        task = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        if not isinstance(task, dict) or not task.get("ready"):
            QMessageBox.information(self, "Command Center", "This task is not eligible to run yet.")
            return
        model = self.model.currentData()
        if not model:
            QMessageBox.warning(self, "Command Center", "No model is available.")
            return
        reviewer = self.reviewer.currentData()
        try:
            record = self.api.start_run(task["task"], model, reviewer)
            self.message.setText(f"Started {record['id']} ({model})")
            self.run_started.emit(record["id"])
        except DaemonError as exc:
            error_box(self, exc)


class RunsWidget(QWidget):
    run_selected = Signal(str)

    def __init__(self, api: DaemonClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.api = api
        self._runs: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._log_offset = 0
        self._log_attempt: int | None = None
        self._log_buffer = ""
        self._loaded: set[str] = set()
        self._views: dict[str, QWidget] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Runs"))
        top.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("ghost")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Run", "Project", "Task", "State", "Try", "Reviewer", "Merge"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMaximumHeight(250)
        self.table.currentCellChanged.connect(self._row_changed)
        layout.addWidget(self.table)

        self.detail = QWidget()
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        header = QHBoxLayout()
        self.title = QLabel("Select a run")
        self.title.setObjectName("value")
        header.addWidget(self.title, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("ghost")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        header.addWidget(self.cancel_button)
        detail_layout.addLayout(header)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        detail_layout.addWidget(self.meta)

        self.tabs = QTabWidget()
        self.diff = DiffView()
        self.tabs.addTab(self.diff, "Diff")
        for kind, label in [
            ("gate", "Gate"),
            ("policy", "Policy"),
            ("review", "Review"),
            ("run", "Run"),
            ("changes", "Changes"),
            ("stderr", "Stderr"),
        ]:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setObjectName("log")
            self.tabs.addTab(view, label)
            self._views[kind] = view
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("log")
        self.tabs.addTab(self.log, "Live log")
        self.tabs.currentChanged.connect(self._tab_changed)
        detail_layout.addWidget(self.tabs, 1)
        layout.addWidget(self.detail, 1)

    def refresh(self) -> None:
        try:
            runs = self.api.runs()
        except DaemonError as exc:
            error_box(self, exc)
            return
        self._runs = runs
        current_id = self._current.get("id") if self._current else None
        self.table.blockSignals(True)
        self.table.setRowCount(len(runs))
        selected_row = -1
        for index, run in enumerate(runs):
            self.table.setItem(index, 0, table_item(run["id"], mono=True))
            self.table.setItem(index, 1, table_item(run.get("project", ""), mono=True))
            self.table.setItem(index, 2, table_item(run.get("task", ""), mono=True))
            state = table_item(run.get("state", ""))
            state.setForeground(status_colour(run.get("state", "")))
            self.table.setItem(index, 3, state)
            self.table.setItem(index, 4, table_item(run.get("attempt", 1)))
            self.table.setItem(index, 5, table_item(run.get("reviewer_verdict") or "—"))
            self.table.setItem(index, 6, table_item((run.get("merge_sha") or "")[:10], mono=True))
            self.table.item(index, 0).setData(Qt.ItemDataRole.UserRole, run["id"])
            if run["id"] == current_id:
                selected_row = index
        self.table.blockSignals(False)
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        elif runs:
            self.table.selectRow(0)
        else:
            self._current = None
            self.detail.setEnabled(False)

    def select_run(self, run_id: str) -> None:
        for index in range(self.table.rowCount()):
            if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == run_id:
                self.table.selectRow(index)
                self._load_run(run_id)
                return
        self.refresh()
        self.select_run(run_id)

    def _row_changed(self, row: int, _column: int, _prev_row: int, _prev_col: int) -> None:
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is not None:
            run_id = item.data(Qt.ItemDataRole.UserRole)
            if run_id and (not self._current or self._current.get("id") != run_id):
                self._load_run(str(run_id))

    def _load_run(self, run_id: str) -> None:
        try:
            run = self.api.run(run_id)
        except DaemonError as exc:
            error_box(self, exc)
            return
        self._current = run
        self._loaded = set()
        self.log.clear()
        self.detail.setEnabled(True)
        self._update_meta()
        active = run.get("state") not in TERMINAL_STATES
        self.cancel_button.setEnabled(active)
        self.tabs.setCurrentIndex(0)
        self._load_tab(0)
        self._reset_log()
        if active:
            self._timer.start()
        else:
            self._timer.stop()

    def _update_meta(self) -> None:
        run = self._current
        if not run:
            return
        self.title.setText(f"RUN {run.get('id')}")
        self.meta.setText(
            f"{run.get('state')}   {run.get('project')} / {run.get('task')}"
            f"   attempt {run.get('attempt')}\n"
            f"coder {run.get('model_resolved') or run.get('model_requested')}"
            f"   reviewer {run.get('reviewer_resolved') or run.get('reviewer_model') or '—'}"
            f"   verdict {run.get('reviewer_verdict') or '—'}"
            f"   merge {(run.get('merge_sha') or '—')[:12]}\n" + "\n".join(run.get("notes") or [])
        )

    def _reset_log(self) -> None:
        if not self._current:
            return
        self.log.clear()
        self._log_buffer = ""
        self._log_offset = 0
        self._log_attempt = None
        while True:
            try:
                chunk = self.api.logs(self._current["id"], stream="agent", offset=self._log_offset)
            except DaemonError:
                return
            text = chunk.get("text", "")
            self._append_log(text)
            previous = self._log_offset
            self._log_offset = int(chunk.get("offset", previous))
            self._log_attempt = chunk.get("attempt")
            if not text or chunk.get("eof") or self._log_offset == previous:
                break

    def _append_log(self, text: str) -> None:
        if not text:
            return
        self._log_buffer += text
        complete = self._log_buffer.splitlines(keepends=True)
        self._log_buffer = ""
        if complete and not complete[-1].endswith(("\n", "\r")):
            self._log_buffer = complete.pop()
        rendered = [self._format_log_line(line.rstrip("\r\n")) for line in complete]
        if rendered:
            self.log.moveCursor(QTextCursor.MoveOperation.End)
            self.log.insertPlainText("\n".join(rendered) + "\n")
            self.log.moveCursor(QTextCursor.MoveOperation.End)

    @staticmethod
    def _format_log_line(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line[:500]
        part = event.get("part") or {}
        part_type = part.get("type")
        if part_type == "text":
            return f"assistant: {part.get('text', '')}"
        if part_type == "tool":
            state = part.get("state") or {}
            value = state.get("input") or {}
            detail = (
                value.get("command")
                or value.get("filePath")
                or value.get("path")
                or value.get("pattern")
                or value.get("description")
                or ""
            )
            output = str(state.get("output") or "").strip().replace("\n", " ")
            text = f"tool {part.get('tool')}: {str(detail)[:240]}"
            if output:
                text += f"\n    {output[:400]}"
            return text
        event_type = event.get("type")
        if event_type in {"step_start", "step_finish"}:
            return f"── {str(event_type).replace('_', ' ')} ──"
        return line[:500]

    def refresh_current(self) -> None:
        if not self._current:
            return
        try:
            run = self.api.run(self._current["id"])
        except DaemonError:
            return
        self._current = run
        self._update_meta()
        active = run.get("state") not in TERMINAL_STATES
        self.cancel_button.setEnabled(active)
        if active:
            self._timer.start()
        else:
            self._timer.stop()
            self._reset_log()

    def _tab_changed(self, index: int) -> None:
        self._load_tab(index)

    def _load_tab(self, index: int) -> None:
        if not self._current:
            return
        label = self.tabs.tabText(index)
        kind = {
            "Diff": "diff",
            "Gate": "gate",
            "Policy": "policy",
            "Review": "review",
            "Run": "run",
            "Changes": "changes",
            "Stderr": "stderr",
        }.get(label)
        if not kind or kind in self._loaded:
            return
        try:
            text = self.api.artifact(self._current["id"], kind)
        except DaemonError:
            text = f"({kind} not available)"
        if kind == "diff":
            self.diff.set_patch(text)
        else:
            view = self._views.get(kind)
            if view is not None:
                view.setPlainText(text)
        self._loaded.add(kind)

    def _poll(self) -> None:
        if not self._current:
            self._timer.stop()
            return
        try:
            run = self.api.run(self._current["id"])
        except DaemonError:
            self._timer.stop()
            return
        self._current = run
        self._update_meta()
        if run.get("state") in TERMINAL_STATES:
            self._timer.stop()
            self.cancel_button.setEnabled(False)
            self._reset_log()
            return
        attempt = run.get("attempt")
        if self._log_attempt != attempt:
            self._reset_log()
            return
        try:
            chunk = self.api.logs(run["id"], stream="agent", offset=self._log_offset)
        except DaemonError:
            return
        self._append_log(chunk.get("text", ""))
        self._log_offset = int(chunk.get("offset", self._log_offset))

    def _cancel(self) -> None:
        if not self._current:
            return
        try:
            self.api.cancel_run(self._current["id"])
        except DaemonError as exc:
            error_box(self, exc)


class SettingsWidget(QWidget):
    def __init__(self, api: DaemonClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.api = api
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Settings"))
        top.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("ghost")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)
        note = QLabel("Read-only. Edit config.yaml and restart the daemon for changes.")
        note.setObjectName("muted")
        layout.addWidget(note)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.form = QVBoxLayout(self.container)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

    def _section(self, title: str) -> QVBoxLayout:
        box = QGroupBox(title)
        box_layout = QVBoxLayout(box)
        self.form.addWidget(box)
        return box_layout

    def refresh(self) -> None:
        try:
            settings = self.api.settings()
            models = self.api.models(force=True)["models"]
        except DaemonError as exc:
            error_box(self, exc)
            return
        while self.form.count():
            item = self.form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        daemon = self._section("Daemon")
        daemon.addWidget(_kv("Version", settings.get("version", "")))
        daemon.addWidget(
            _kv(
                "Bind",
                f"{settings.get('daemon', {}).get('bind')}:"
                f"{settings.get('daemon', {}).get('port')}",
            )
        )
        daemon.addWidget(_kv("TaskStore", settings.get("taskstore", {}).get("url", "")))
        daemon.addWidget(_kv("Autonomy", settings.get("autonomy", "")))
        paths = self._section("Paths")
        path_values = settings.get("paths", {})
        for label, key in [
            ("State", "state_dir"),
            ("Worktrees", "worktree_root"),
            ("Token", "token_path"),
        ]:
            paths.addWidget(_kv(label, str(path_values.get(key, ""))))
        paths.addWidget(_kv("Token present", "yes" if path_values.get("token_exists") else "no"))
        opencode = self._section("OpenCode")
        opencode.addWidget(_kv("Binary", settings.get("opencode", {}).get("binary", "")))
        opencode.addWidget(_kv("Agents", settings.get("opencode", {}).get("agents_dir", "")))
        budgets = self._section("Budgets and policy")
        budget_values = settings.get("budgets", {})
        budgets.addWidget(_kv("Max attempts", budget_values.get("max_attempts", "")))
        budgets.addWidget(_kv("Gate timeout", f"{budget_values.get('gate_timeout_seconds', '')} s"))
        policy_values = settings.get("policy", {})
        budgets.addWidget(_kv("Max files", policy_values.get("max_files", "")))
        budgets.addWidget(_kv("Max diff bytes", policy_values.get("max_diff_bytes", "")))
        roles = settings.get("roles", {})
        role_box = self._section("Roles")
        for role in ("inquisitor", "coder", "reviewer", "chat"):
            role_box.addWidget(_kv(role, roles.get(role, "—")))
        model_box = self._section("Models")
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["ID", "Label", "Model", "Up", "Reason"])
        table.horizontalHeader().setSectionResizeMode(
            2, table.horizontalHeader().ResizeMode.Stretch
        )
        table.horizontalHeader().setSectionResizeMode(
            4, table.horizontalHeader().ResizeMode.Stretch
        )
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(models))
        for row, model in enumerate(models):
            table.setItem(row, 0, table_item(model.get("id"), mono=True))
            table.setItem(row, 1, table_item(model.get("label")))
            table.setItem(row, 2, table_item(model.get("model"), mono=True))
            up = table_item("yes" if model.get("up") else "no")
            up.setForeground(QColor(GOOD if model.get("up") else MUT))
            table.setItem(row, 3, up)
            table.setItem(row, 4, table_item(model.get("reason")))
        model_box.addWidget(table)
        self.form.addStretch(1)


def _kv(label: str, value: Any) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    left = QLabel(label)
    left.setObjectName("muted")
    left.setMinimumWidth(130)
    right = QLabel(str(value))
    right.setObjectName("value")
    right.setWordWrap(True)
    layout.addWidget(left)
    layout.addWidget(right, 1)
    return row
