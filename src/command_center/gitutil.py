from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import CommandCenterError


class GitError(CommandCenterError):
    pass


def _run(
    root: Path, *args: str, check: bool = True, text: bool = True
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    if check and result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        raise GitError(stderr.strip() or f"git {' '.join(args)} failed")
    return result


def is_git_repo(root: Path) -> bool:
    result = _run(root, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def head_commit(root: Path, ref: str = "HEAD") -> str:
    result = _run(root, "rev-parse", ref)
    return result.stdout.strip()


def branch_exists(root: Path, name: str) -> bool:
    result = _run(root, "rev-parse", "--verify", "--quiet", name, check=False)
    return result.returncode == 0


def base_ref(root: Path) -> str:
    return "cc/integration" if branch_exists(root, "cc/integration") else "HEAD"


def status_porcelain(root: Path, paths: list[str] | None = None) -> str:
    args = ["status", "--porcelain", "--untracked-files=normal"]
    if paths:
        args.append("--")
        args.extend(paths)
    return _run(root, *args).stdout


def is_clean(root: Path, paths: list[str] | None = None) -> bool:
    return not status_porcelain(root, paths).strip()


def file_tracked(root: Path, path: str) -> bool:
    result = _run(root, "ls-files", "--error-unmatch", "--", path, check=False)
    return result.returncode == 0


def blob(root: Path, ref: str, path: str) -> bytes:
    result = _run(root, "show", f"{ref}:{path}", text=False)
    return result.stdout


def add_all(root: Path) -> None:
    _run(root, "add", "-A")


def staged_names(root: Path, base: str) -> list[str]:
    result = _run(root, "diff", "--cached", "--name-only", base)
    return [line for line in result.stdout.splitlines() if line.strip()]


def staged_numstat(root: Path, base: str) -> list[tuple[int | None, int | None, str]]:
    result = _run(root, "diff", "--cached", "--numstat", base)
    rows: list[tuple[int | None, int | None, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        rows.append(
            (
                None if added == "-" else int(added),
                None if deleted == "-" else int(deleted),
                path,
            )
        )
    return rows


def staged_diff(root: Path, base: str) -> str:
    return _run(root, "diff", "--cached", base).stdout


def staged_added_lines(root: Path, base: str) -> str:
    return _run(root, "diff", "--cached", "-U0", base).stdout


def commit(root: Path, message: str) -> str:
    _run(
        root,
        "-c",
        "user.name=Command Center",
        "-c",
        "user.email=command-center@localhost",
        "commit",
        "-m",
        message,
    )
    return head_commit(root)


def reset_hard(root: Path, ref: str = "HEAD") -> None:
    _run(root, "reset", "--hard", ref)


def clean(root: Path) -> None:
    _run(root, "clean", "-fdx")


def ensure_integration(root: Path) -> str:
    if not branch_exists(root, "cc/integration"):
        _run(root, "branch", "cc/integration", "HEAD")
    return head_commit(root, "cc/integration")


def move_branch(root: Path, branch: str, target: str) -> None:
    _run(root, "branch", "-f", branch, target)


def worktree_add(root: Path, path: Path, branch: str, base: str) -> None:
    _run(root, "worktree", "add", "-b", branch, str(path), base)


def worktree_remove(root: Path, path: Path) -> None:
    """Remove a worktree, including a stale directory git no longer tracks.

    Build worktrees are reused per work item, so a path can survive a run that
    was blocked before cleanup. `git worktree add` refuses a non-empty path, so
    the directory must really be gone.
    """
    try:
        if path.resolve() == root.resolve():
            return
    except OSError:
        return
    _run(root, "worktree", "remove", "--force", str(path), check=False)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    _run(root, "worktree", "prune", check=False)


def worktree_config(root: Path, worktree: Path, key: str, value: str) -> None:
    _run(root, "config", "extensions.worktreeConfig", "true")
    _run(worktree, "config", "--worktree", key, value)


def status_short(root: Path) -> str:
    return _run(root, "status", "--porcelain", "--untracked-files=normal").stdout


def current_branch(root: Path) -> str:
    return _run(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def diff_patch(root: Path, left: str, right: str) -> str:
    return _run(
        root,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "-M",
        f"{left}..{right}",
    ).stdout


