from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import gitutil
from .config import PolicyConfig

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), "private key block"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
            r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
        ),
        "provider token",
    ),
    (
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*"
            r"['\"][^'\"]{12,}['\"]"
        ),
        "credential assignment",
    ),
    (
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(?:API_KEY|_TOKEN|_SECRET|_PASSWORD)\s*=\s*\S+"
        ),
        "environment credential",
    ),
]
BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".mp3",
    ".ogg",
    ".wav",
    ".mp4",
    ".mov",
    ".so",
    ".dll",
    ".dylib",
    ".pyc",
}
SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx"}


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    path: str
    detail: str


@dataclass
class PolicyResult:
    passed: bool
    files: int
    bytes: int
    findings: list[PolicyFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "files": self.files,
            "bytes": self.bytes,
            "findings": [
                {"code": finding.code, "path": finding.path, "detail": finding.detail}
                for finding in self.findings
            ],
        }


def _matches_scope(path: str, scope: list[str]) -> bool:
    pure = PurePosixPath(path)
    return any(
        pure.match(pattern) or fnmatch.fnmatchcase(path, pattern) for pattern in scope
    )


def scan(
    root: Path,
    base: str,
    *,
    scope: list[str],
    config: PolicyConfig,
) -> PolicyResult:
    findings: list[PolicyFinding] = []
    stat = gitutil.staged_numstat(root, base)
    diff_text = gitutil.staged_diff(root, base)
    files = len(stat)
    size = len(diff_text.encode("utf-8"))

    for added, deleted, path in stat:
        name = PurePosixPath(path).name
        if added is None or deleted is None:
            findings.append(
                PolicyFinding("policy_binary", path, "binary file in the diff")
            )
        if name == ".env" or name.startswith(".env."):
            findings.append(
                PolicyFinding("policy_env", path, "environment file in the diff")
            )
        if path == "command-center.yaml" or path.startswith("docs/spec/"):
            findings.append(
                PolicyFinding(
                    "policy_spec", path, "spec or gate file changed during Build"
                )
            )
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in BINARY_EXTENSIONS:
            findings.append(
                PolicyFinding("policy_binary", path, f"{suffix} file in the diff")
            )
        if suffix in SECRET_EXTENSIONS:
            findings.append(
                PolicyFinding("policy_secret", path, "key material in the diff")
            )
        if scope and not _matches_scope(path, scope):
            findings.append(
                PolicyFinding("policy_scope", path, "file outside the task scope")
            )

    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    PolicyFinding(
                        "policy_secret",
                        current or "?",
                        f"possible {label} in an added line",
                    )
                )

    if files > config.max_files:
        findings.append(
            PolicyFinding(
                "policy_size",
                ".",
                f"{files} changed files exceeds {config.max_files}",
            )
        )
    if size > config.max_diff_bytes:
        findings.append(
            PolicyFinding(
                "policy_size",
                ".",
                f"{size} diff bytes exceeds {config.max_diff_bytes}",
            )
        )

    deduped: dict[tuple[str, str, str], PolicyFinding] = {}
    for finding in findings:
        deduped.setdefault(
            (finding.code, finding.path, finding.detail), finding
        )
    return PolicyResult(
        passed=not deduped,
        files=files,
        bytes=size,
        findings=list(deduped.values()),
    )
