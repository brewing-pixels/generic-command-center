from __future__ import annotations

from command_center.review import ReviewVerdict, parse_review


def test_parse_valid_review() -> None:
    result = parse_review(
        '{"verdict": "approve", "reasons": ["ok"], "findings": []}'
    )
    assert result.verdict == ReviewVerdict.approve
    assert result.reasons == ["ok"]


def test_parse_fenced_review() -> None:
    result = parse_review(
        '```json\n{"verdict":"changes","reasons":["fix it"],'
        '"findings":[{"file":"a.py","line":1,"severity":"warn","note":"x"}]}\n```'
    )
    assert result.verdict == ReviewVerdict.changes
    assert result.findings[0].file == "a.py"


def test_invalid_reviews_become_changes() -> None:
    assert parse_review("not json").verdict == ReviewVerdict.changes
    assert (
        parse_review('{"verdict": "unknown", "reasons": ["x"]}').verdict
        == ReviewVerdict.changes
    )
    assert (
        parse_review('{"verdict": "approve", "reasons": []}').verdict
        == ReviewVerdict.changes
    )
    assert (
        parse_review(
            '{"verdict": "approve", "reasons": ["ok"], '
            '"findings": [{"severity": "block", "note": "bad"}]}'
        ).verdict
        == ReviewVerdict.changes
    )
