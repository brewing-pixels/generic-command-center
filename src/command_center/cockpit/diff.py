from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffLine:
    kind: str
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class DiffHunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class DiffFile:
    old_path: str = ""
    new_path: str = ""
    hunks: list[DiffHunk] = field(default_factory=list)
    binary: bool = False

    @property
    def display_name(self) -> str:
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        return self.old_path or "unknown"

    @property
    def additions(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "add")

    @property
    def deletions(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.kind == "del")


@dataclass
class SideRow:
    left_no: int | None
    left: str
    right_no: int | None
    right: str
    kind: str


def _clean_path(value: str) -> str:
    value = value.strip()
    if value == "/dev/null":
        return value
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def parse_patch(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: DiffHunk | None = None
    old_no = 0
    new_no = 0

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("diff --git "):
            parts = line.split(" ")
            current = DiffFile(
                old_path=_clean_path(parts[2]) if len(parts) > 2 else "",
                new_path=_clean_path(parts[3]) if len(parts) > 3 else "",
            )
            files.append(current)
            hunk = None
            continue
        if line.startswith("--- "):
            if current is None:
                current = DiffFile()
                files.append(current)
            current.old_path = _clean_path(line[4:])
            hunk = None
            continue
        if line.startswith("+++ "):
            if current is None:
                current = DiffFile()
                files.append(current)
            current.new_path = _clean_path(line[4:])
            hunk = None
            continue
        if line.startswith("Binary files ") and current is not None:
            current.binary = True
            hunk = None
            continue
        match = HUNK_RE.match(line)
        if match and current is not None:
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            hunk = DiffHunk(header=line)
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("\\ No newline"):
            continue
        if line.startswith("+"):
            hunk.lines.append(DiffLine("add", line[1:], None, new_no))
            new_no += 1
        elif line.startswith("-"):
            hunk.lines.append(DiffLine("del", line[1:], old_no, None))
            old_no += 1
        elif line.startswith(" "):
            hunk.lines.append(DiffLine("context", line[1:], old_no, new_no))
            old_no += 1
            new_no += 1
        elif line.startswith("@@"):
            continue
    return files


def side_rows(hunk: DiffHunk) -> list[SideRow]:
    rows: list[SideRow] = [SideRow(None, hunk.header, None, "", "hunk")]
    lines = hunk.lines
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.kind == "context":
            rows.append(SideRow(line.old_no, line.text, line.new_no, line.text, "context"))
            index += 1
            continue
        deleted: list[DiffLine] = []
        added: list[DiffLine] = []
        while index < len(lines) and lines[index].kind in {"del", "add"}:
            if lines[index].kind == "del":
                deleted.append(lines[index])
            else:
                added.append(lines[index])
            index += 1
        depth = max(len(deleted), len(added))
        for offset in range(depth):
            left = deleted[offset] if offset < len(deleted) else None
            right = added[offset] if offset < len(added) else None
            kind = "replace" if left and right else ("del" if left else "add")
            rows.append(
                SideRow(
                    left.old_no if left else None,
                    left.text if left else "",
                    right.new_no if right else None,
                    right.text if right else "",
                    kind,
                )
            )
    return rows
