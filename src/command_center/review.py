from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class ReviewVerdict(StrEnum):
    approve = "approve"
    changes = "changes"
    block = "block"


class ReviewFinding(BaseModel):
    file: str | None = None
    line: int | None = None
    severity: Literal["block", "warn", "note"] = "warn"
    note: str = ""


class ReviewResult(BaseModel):
    verdict: ReviewVerdict
    reasons: list[str] = Field(min_length=1, max_length=5)
    findings: list[ReviewFinding] = Field(default_factory=list)


def _changes(detail: str) -> ReviewResult:
    return ReviewResult(
        verdict=ReviewVerdict.changes,
        reasons=[detail],
        findings=[],
    )


def parse_review(text: str) -> ReviewResult:
    text = text.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return _changes("reviewer did not return a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return _changes(f"reviewer JSON is invalid: {exc}")
    if not isinstance(data, dict):
        return _changes("reviewer JSON is not an object")
    try:
        result = ReviewResult.model_validate(data)
    except ValidationError as exc:
        return _changes(f"reviewer JSON does not match the schema: {exc}")
    if result.verdict == ReviewVerdict.approve and any(
        finding.severity == "block" for finding in result.findings
    ):
        return _changes("reviewer approved while reporting a block finding")
    return result
