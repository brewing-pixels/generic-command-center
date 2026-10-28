from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Any

import httpx
from PySide6.QtCore import QLockFile, QSettings, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..client import DaemonClient, DaemonError
from ..config import Config, load_config
from ..token import ensure_token
from .theme import apply_theme
from .widgets import (
    AgendaWidget,
    QueueWidget,
    RunsWidget,
    SettingsWidget,
    make_icon,
)


class EventStream(QThread):
    event = Signal(dict)
    connection = Signal(bool, str)

    def __init__(self, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        host = "127.0.0.1" if config.daemon.bind in ("0.0.0.0", "::") else config.daemon.bind
        self.base_url = f"http://{host}:{config.daemon.port}"
        self.token = ensure_token(config.token_path)
        self._stop = threading.Event()
        self._last_id: int | None = None
        self._client: httpx.Client | None = None

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if self.isRunning():
            self.terminate()

    def run(self) -> None:  # noqa: D401
        while not self._stop.is_set():
            headers = {"Authorization": f"Bearer {self.token}"}
            if self._last_id is not None:
                headers["Last-Event-ID"] = str(self._last_id)
            try:
                timeout = httpx.Timeout(10.0, read=None)
                with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
                    with client.stream("GET", "/api/events", headers=headers) as response:
                        if response.status_code != 200:
                            raise RuntimeError(f"event stream returned {response.status_code}")
                        self.connection.emit(True, "")
                        event_name = ""
                        event_id: int | None = None
                        data_lines: list[str] = []
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            if line == "":
                                if event_name and data_lines:
                                    try:
                                        payload = json.loads("\n".join(data_lines))
                                    except json.JSONDecodeError:
                                        payload = {}
                                    self.event.emit(
                                        {
                                            "event": event_name,
                                            "data": payload,
                                            "id": event_id,
                                        }
                                    )
                                    if event_id is not None:
                                        self._last_id = event_id
                                event_name = ""
                                event_id = None
                                data_lines = []
                            elif line.startswith(":"):
                                continue
                            elif line.startswith("id:"):
                                raw = line[3:].strip()
                                event_id = int(raw) if raw.isdigit() else event_id
                            elif line.startswith("event:"):
                                event_name = line[6:].strip()
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].lstrip())
                self._client = None
                self.connection.emit(False, "stream closed")
            except Exception as exc:  # noqa: BLE001 - surface to status bar
                self._client = None
                if self._stop.is_set():
                    return
                self.connection.emit(False, str(exc))
            self._stop.wait(2.0)


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = DaemonClient(config)
        self._quitting = False
        self._last_notification_run: str | None = None
        self.settings_store = QSettings("CommandCenter", "Cockpit")
        self._notified = set(self.settings_store.value("notified", []) or [])
        self.setWindowTitle("Command Center")
        self.setWindowIcon(make_icon())
        self.resize(1440, 920)
        self.setMinimumSize(1080, 700)
        self._build()
        self._build_tray()
        self._events = EventStream(config, self)
        self._events.event.connect(self._on_event)
        self._events.connection.connect(self._on_connection)
        self._events.start()
        QTimer.singleShot(800, self._refresh_lists)
        QTimer.singleShot(1500, self._reconcile_blocked)

    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 12)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        titles = QVBoxLayout()
        kicker = QLabel("EXECUTION // LAYER")
        kicker.setObjectName("kicker")
        title = QLabel("COMMAND CENTER")
        title.setObjectName("title")
        titles.addWidget(kicker)
        titles.addWidget(title)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)
        self.connection_label = QLabel("CONNECTING")
        self.connection_label.setObjectName("muted")
        header_layout.addWidget(self.connection_label)
        layout.addWidget(header)

        self.tabs = QTabWidget()
        self.agenda = AgendaWidget(self.api)
        self.queue = QueueWidget(self.api)
        self.runs = RunsWidget(self.api)
        self.settings = SettingsWidget(self.api)
        self.tabs.addTab(self.agenda, "Agenda")
        self.tabs.addTab(self.queue, "Queue")
        self.tabs.addTab(self.runs, "Runs")
        self.tabs.addTab(self.settings, "Settings")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.queue.run_started.connect(self.open_run)
        self.statusBar().showMessage("Connecting to the daemon…")
        self.tabs.currentChanged.connect(self._tab_changed)

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QApplication.setQuitOnLastWindowClosed(True)
            return
        QApplication.setQuitOnLastWindowClosed(False)
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("Command Center")
        menu = QMenu(self)
        open_action = QAction("Open cockpit", self)
        open_action.triggered.connect(self.show_window)
        menu.addAction(open_action)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._notification_clicked)
        self.tray.show()

    def _tab_changed(self, _index: int) -> None:
        widget = self.tabs.currentWidget()
        if widget is self.queue:
            self.queue.refresh()
        elif widget is self.runs:
            self.runs.refresh()

    def _refresh_lists(self) -> None:
        self.queue.refresh()
        self.runs.refresh()

    def _reconcile_blocked(self) -> None:
        try:
            runs = self.api.runs(state="blocked")
        except DaemonError as exc:
            self.statusBar().showMessage(str(exc))
            return
        for run in runs:
            self._notify(
                "run.blocked",
                {
                    "run_id": run.get("id"),
                    "code": run.get("verdict") or "blocked",
                },
            )

    def _on_connection(self, online: bool, reason: str) -> None:
        self.connection_label.setText("ONLINE" if online else "OFFLINE")
        self.connection_label.setStyleSheet(f"color: {'#34d399' if online else '#ef6a6a'};")
        if online:
            self.statusBar().showMessage("daemon online")
        else:
            self.statusBar().showMessage(f"daemon offline: {reason}")

    def _on_event(self, payload: dict[str, Any]) -> None:
        event = str(payload.get("event", ""))
        data = payload.get("data") or {}
        if event in {
            "run.started",
            "run.finished",
            "run.merged",
            "run.blocked",
            "gate.failed",
            "policy.failed",
            "review.verdict",
        }:
            self.runs.refresh()
            self.runs.refresh_current()
            self.queue.refresh()
        if event in {"agenda.locked", "agenda.unlocked"}:
            self.agenda.refresh()
        if event == "model.down":
            self.statusBar().showMessage(f"model {data.get('id')} down: {data.get('reason')}")
        self._notify(event, data)

    def _notify(self, event: str, data: dict[str, Any]) -> None:
        if self.tray is None:
            return
        run_id = data.get("run_id")
        key = ""
        text = ""
        if event == "gate.failed":
            key = f"gate:{run_id}"
            text = f"Gate failed for run {run_id}"
        elif event == "policy.failed":
            key = f"policy:{run_id}"
            text = f"Policy failed for run {run_id}"
        elif event == "run.blocked":
            key = f"blocked:{run_id}"
            text = f"Run {run_id} blocked: {data.get('code', 'blocked')}"
        if key and key in self._notified:
            return
        if key:
            self._notified.add(key)
            self.settings_store.setValue("notified", sorted(self._notified)[-200:])
        self._last_notification_run = str(run_id) if run_id else None
        self.tray.showMessage(
            "Command Center",
            text,
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )

    def _notification_clicked(self) -> None:
        if self._last_notification_run:
            self.open_run(self._last_notification_run)
        else:
            self.show_window()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_run(self, run_id: str) -> None:
        self.show_window()
        self.tabs.setCurrentWidget(self.runs)
        self.runs.refresh()
        self.runs.select_run(run_id)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting or self.tray is None:
            self.quit()
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "Command Center",
            "Running in the tray. Use the tray menu to quit.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._events.stop()
        self._events.wait(3000)
        if self.tray is not None:
            self.tray.hide()
        self.api.close()
        QApplication.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Command Center native cockpit")
    parser.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - shown before QApplication exists
        print(f"command center: {exc}", file=sys.stderr)
        return 1
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Command Center")
    app.setApplicationDisplayName("Command Center")
    app.setOrganizationName("CommandCenter")
    app.setDesktopFileName("command-center-cockpit")
    apply_theme(app)
    probe = DaemonClient(config)
    try:
        probe.health()
    except DaemonError as exc:
        probe.close()
        QMessageBox.critical(
            None,
            "Command Center",
            f"Daemon unavailable: {exc}\nStart command-center.service first.",
        )
        return 1
    probe.close()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config.state_dir / "cockpit.lock"))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(100):
        QMessageBox.warning(
            None,
            "Command Center",
            "The cockpit is already running.",
        )
        return 1
    try:
        window = MainWindow(config)
    except Exception as exc:  # noqa: BLE001 - startup surface
        QMessageBox.critical(None, "Command Center", str(exc))
        lock.unlock()
        return 1
    window.show()
    try:
        return app.exec()
    finally:
        window._events.stop()
        window._events.wait(3000)
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
