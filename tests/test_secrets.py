"""The commit guard: what `.githooks/secret-scan.sh` must refuse and allow."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".githooks" / "secret-scan.sh"


def scratch(tmp_path: Path) -> Path:
    """A throwaway repository carrying only the scanner."""
    root = tmp_path / "repo"
    (root / ".githooks").mkdir(parents=True)
    shutil.copy(SCRIPT, root / ".githooks" / "secret-scan.sh")
    (tmp_path / "home").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def stage(root: Path, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def scan(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the scanner with a private HOME, so a developer's config cannot leak in."""
    env = {**os.environ, "HOME": str(tmp_path / "home"), "CC_CONFIG": ""}
    return subprocess.run(
        ["bash", ".githooks/secret-scan.sh", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_placeholder_pin_is_allowed(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "config.example.yaml", 'daemon:\n  pin: "0000"\n')
    assert scan(root, tmp_path, "--tree").returncode == 0


def test_a_real_pin_is_refused(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "deploy.yaml", 'daemon:\n  pin: "9182"\n')
    result = scan(root, tmp_path, "--tree")
    assert result.returncode == 1
    assert "real PIN" in result.stderr


def test_tests_may_use_a_fixture_pin(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "tests/test_daemon.py", 'update={"pin": "2468"}\n')
    assert scan(root, tmp_path, "--tree").returncode == 0


def test_local_config_must_not_be_tracked(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "config.yaml", 'daemon:\n  pin: "0000"\n')
    result = scan(root, tmp_path, "--staged")
    assert result.returncode == 1
    assert "must not be tracked" in result.stderr


def test_key_shaped_string_is_refused(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "settings.py", 'token = "sk-1234567890abcdefghijklmn"\n')  # allowlist-secret
    result = scan(root, tmp_path, "--tree")
    assert result.returncode == 1
    assert "possible credential" in result.stderr


def test_allowlist_marker_silences_a_fixture(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stage(root, "tests/test_policy.py", 'KEY = "sk-1234567890abcdefghijklmn"  # allowlist-secret\n')
    assert scan(root, tmp_path, "--tree").returncode == 0


def test_the_live_pin_is_refused_in_a_message(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    (root / "config.yaml").write_text('daemon:\n  pin: "9182"\n', encoding="utf-8")
    message = tmp_path / "msg"
    message.write_text("chore: rotate\n\nThe PIN is 9182.\n", encoding="utf-8")
    result = scan(root, tmp_path, "--message", str(message))
    assert result.returncode == 1
    assert "PIN" in result.stderr
