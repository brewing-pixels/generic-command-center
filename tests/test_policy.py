from __future__ import annotations

from pathlib import Path

from helpers import git

from command_center import gitutil, policy
from command_center.config import PolicyConfig


def staged_scan(repo: Path, scope: list[str], config: PolicyConfig | None = None):
    gitutil.add_all(repo)
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    return policy.scan(
        repo,
        base,
        scope=scope,
        config=config or PolicyConfig(),
    )


def test_clean_change_passes(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("print('hello')\n", encoding="utf-8")
    result = staged_scan(repo, ["src/**"])
    assert result.passed
    assert result.findings == []
    assert result.files == 1


def test_env_and_secret_are_blocked(repo: Path) -> None:
    (repo / ".env").write_text("A=1\n", encoding="utf-8")
    result = staged_scan(repo, [])
    assert not result.passed
    assert "policy_env" in {finding.code for finding in result.findings}

    (repo / ".env").unlink()
    (repo / "config.py").write_text(
        'token = "sk-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8"  # allowlist-secret
    )
    result = staged_scan(repo, [])
    assert not result.passed
    assert "policy_secret" in {finding.code for finding in result.findings}


def test_spec_gate_and_binary_are_blocked(repo: Path) -> None:
    (repo / "docs" / "spec").mkdir(parents=True)
    (repo / "docs" / "spec" / "NEW.md").write_text("x\n", encoding="utf-8")
    (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    result = staged_scan(repo, [])
    codes = {finding.code for finding in result.findings}
    assert "policy_spec" in codes
    assert "policy_binary" in codes


def test_scope_and_size(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "x.py").write_text("x\n", encoding="utf-8")
    result = staged_scan(repo, ["src/**"])
    assert "policy_scope" in {finding.code for finding in result.findings}

    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    result = staged_scan(repo, [], PolicyConfig(max_files=1))
    assert "policy_size" in {finding.code for finding in result.findings}
