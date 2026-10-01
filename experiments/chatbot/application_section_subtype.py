"""Small diagnostic for application-query section subtypes.

This is an experiment helper, not a production router. It exists to test whether
the gold-set application questions contain enough lexical signal to distinguish
"how/where to apply" from "when to apply" before changing retrieval policy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


CASES_PATH = Path(__file__).with_name("application_section_cases.jsonl")

_PERIOD_RE = re.compile(
    r"(언제\s*신청|신청\s*기간|접수\s*기간|모집\s*기간|기간\s*없이|마감|상시\s*(?:모집|신청)|신청\s*기한|접수\s*기한)"
)
_METHOD_RE = re.compile(
    r"(어디|어떤\s*기관|어떻게|신청|문의|온라인|주민센터|보건소|카드사|공단)"
)


def classify_application_subtype(query: str) -> str:
    normalized = " ".join(str(query or "").split())
    if _PERIOD_RE.search(normalized):
        return "period"
    if _METHOD_RE.search(normalized):
        return "method"
    return "unknown"


def expected_application_subtype(expected_sections: list[str]) -> str:
    sections = set(expected_sections)
    if "신청 기간" in sections:
        return "period"
    if "신청 방법" in sections:
        return "method"
    return "unknown"


def load_application_cases(path: Path = CASES_PATH) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("category") == "application":
            cases.append(payload)
    return cases


def evaluate(path: Path = CASES_PATH) -> dict:
    cases = load_application_cases(path)
    rows = []
    correct = 0
    for case in cases:
        expected = expected_application_subtype(case["expected_sections"])
        predicted = classify_application_subtype(case["query"])
        is_correct = predicted == expected
        correct += int(is_correct)
        rows.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "expected": expected,
                "predicted": predicted,
                "correct": is_correct,
            }
        )
    return {
        "case_count": len(rows),
        "correct_count": correct,
        "accuracy_pct": round((correct / len(rows) * 100) if rows else 0.0, 2),
        "cases": rows,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
