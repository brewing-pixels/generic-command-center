from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import gitutil
from .errors import SpecError, TaskStoreError
from .paths import project_root
from .taskstore import TaskStoreClient, TaskStoreProject

DEFAULT_SPEC = "docs/spec/SPEC.md"
DEFAULT_DESIGN = "docs/spec/DESIGN.md"


@dataclass(frozen=True)
class SpecCheck:
    ok: bool
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SpecApproval:
    project_id: str
    docs: list[str]
    digest: str
    commit: str


def default_docs(root: Path) -> list[str]:
    if (root / DEFAULT_SPEC).is_file():
        docs = [DEFAULT_SPEC]
        if (root / DEFAULT_DESIGN).is_file():
            docs.append(DEFAULT_DESIGN)
        return docs
    if (root / "SPEC.md").is_file():
        docs = ["SPEC.md"]
        if (root / "DESIGN.md").is_file():
            docs.append("DESIGN.md")
        return docs
    return [DEFAULT_SPEC]


def normalize_docs(root: Path, docs: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in docs:
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise SpecError(f"spec doc is outside the project: {raw}") from exc
        if ".." in path.parts:
            raise SpecError(f"spec doc escapes the project: {raw}")
        text = path.as_posix()
        if text not in normalized:
            normalized.append(text)
    if not normalized:
        raise SpecError("at least one spec doc is required")
    return normalized


def compute_digest(root: Path, docs: list[str], ref: str) -> str:
    lines: list[str] = []
    for doc in sorted(docs):
        try:
            content = gitutil.blob(root, ref, doc)
        except gitutil.GitError as exc:
            raise SpecError(f"{doc} is not present in {ref}: {exc}") from exc
        lines.append(f"{doc}\0{hashlib.sha256(content).hexdigest()}\n")
    manifest = "".join(lines).encode("utf-8")
    return "sha256:" + hashlib.sha256(manifest).hexdigest()


def check(project: TaskStoreProject) -> SpecCheck:
    state = project.spec_state
    if state is None or not state.docs:
        return SpecCheck(False, "spec_unapproved", "no approved spec docs")
    if state.phase not in ("approved", "building"):
        return SpecCheck(False, "spec_unapproved", f"phase is {state.phase}")
    root = project_root(project.path)
    if root is None:
        return SpecCheck(False, "spec_unapproved", "project has no path")
    try:
        if not gitutil.is_git_repo(root):
            return SpecCheck(False, "spec_unapproved", "project is not a git repo")
        ref = gitutil.base_ref(root)
        digest = compute_digest(root, state.docs, ref)
    except (gitutil.GitError, SpecError) as exc:
        return SpecCheck(False, "spec_dirty", str(exc))
    if state.dirty:
        return SpecCheck(False, "spec_dirty", "spec state is marked dirty")
    if digest != state.digest:
        return SpecCheck(False, "spec_dirty", "spec docs changed since approval")
    return SpecCheck(True)


def approve(
    taskstore: TaskStoreClient,
    project_id: str,
    docs: list[str] | None = None,
) -> SpecApproval:
    project = taskstore.get_project(project_id)
    root = project_root(project.path)
    if root is None:
        raise SpecError("project has no path")
    try:
        if not gitutil.is_git_repo(root):
            raise SpecError(f"{root} is not a git repository")
    except gitutil.GitError as exc:
        raise SpecError(str(exc)) from exc

    chosen = docs
    if not chosen and project.spec_state and project.spec_state.docs:
        chosen = project.spec_state.docs
    if not chosen:
        chosen = default_docs(root)
    normalized = normalize_docs(root, chosen)

    for doc in normalized:
        path = root / doc
        if not path.is_file():
            raise SpecError(f"spec doc does not exist: {doc}")
        if not gitutil.file_tracked(root, doc):
            raise SpecError(f"spec doc is not committed: {doc}")
        if not gitutil.is_clean(root, [doc]):
            raise SpecError(f"spec doc has uncommitted changes: {doc}")

    try:
        ref = gitutil.base_ref(root)
        commit = gitutil.head_commit(root, ref)
        digest = compute_digest(root, normalized, ref)
    except gitutil.GitError as exc:
        raise SpecError(str(exc)) from exc

    approved_at = datetime.now(UTC).replace(microsecond=0)
    payload = {
        "spec_state": {
            "docs": normalized,
            "digest": digest,
            "approved_at": approved_at.isoformat().replace("+00:00", "Z"),
            "approved_in_commit": commit,
            "dirty": False,
            "phase": "approved",
        },
        "if_rev": project.rev,
    }
    try:
        taskstore.patch_project(project_id, payload)
    except TaskStoreError as exc:
        if exc.status_code != 409:
            raise
        latest = taskstore.get_project(project_id)
        payload["if_rev"] = latest.rev
        taskstore.patch_project(project_id, payload)
    return SpecApproval(project_id, normalized, digest, commit)
