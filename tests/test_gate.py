from __future__ import annotations

import sys
from pathlib import Path

from command_center.gate import run_gate


def test_gate_pass_and_fail(tmp_path: Path) -> None:
    gate_path = tmp_path / "gate.json"
    passed = run_gate(
        tmp_path,
        [["true"]],
        timeout=10,
        gate_path=gate_path,
    )
    assert passed.passed
    assert passed.commands[0].exit_code == 0
    assert gate_path.is_file()

    failed = run_gate(
        tmp_path,
        [["false"]],
        timeout=10,
        gate_path=gate_path,
    )
    assert not failed.passed
    assert failed.commands[0].exit_code != 0


def test_gate_scrubs_provider_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_API_KEY", "secret-value")
    result = run_gate(
        tmp_path,
        [
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('PROVIDER_API_KEY', 'MISSING'))",
            ]
        ],
        timeout=10,
        gate_path=tmp_path / "gate.json",
    )
    assert result.passed
    assert "MISSING" in result.commands[0].tail
    assert "secret-value" not in result.commands[0].tail


def test_gate_writes_verbatim_result(tmp_path: Path) -> None:
    path = tmp_path / "gate.json"
    run_gate(
        tmp_path,
        [[sys.executable, "-c", "print('hello')"]],
        timeout=10,
        gate_path=path,
    )
    text = path.read_text(encoding="utf-8")
    assert "hello" in text
    assert '"passed": true' in text
