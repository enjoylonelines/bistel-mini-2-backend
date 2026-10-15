"""Validate the review state of the policy-quality gold-set candidate file.

The file deliberately separates proposed labels from reviewed gold labels. It
must not be used to publish accuracy metrics until every row is approved by a
policy reviewer with its source evidence recorded.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
DEFAULT_DATASET = BASE / "datasets" / "dodam_policy_quality_v1.jsonl"
ALLOWED_AXES = {"application_method", "benefit_content", "eligibility"}
ALLOWED_DECISIONS = {"RECOMMEND", "ASK_FOLLOW_UP", "REJECT"}
ALLOWED_REVIEW_STATUSES = {"approved", "needs_policy_review", "rejected"}


def load_goldset_candidates(path: Path = DEFAULT_DATASET) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len({row.get("case_id") for row in rows}) != len(rows):
        raise ValueError("case_id values must be unique")
    for index, row in enumerate(rows, start=1):
        required = {
            "case_id", "query", "quality_axis", "expected_policy_ids",
            "expected_sections", "expected_decision", "label_origin", "review_status",
        }
        if missing := required - row.keys():
            raise ValueError(f"case {index} missing fields: {sorted(missing)}")
        if row["quality_axis"] not in ALLOWED_AXES:
            raise ValueError(f"case {row['case_id']} has unsupported quality_axis")
        if row["expected_decision"] not in ALLOWED_DECISIONS:
            raise ValueError(f"case {row['case_id']} has unsupported decision")
        if row["review_status"] not in ALLOWED_REVIEW_STATUSES:
            raise ValueError(f"case {row['case_id']} has unsupported review_status")
        if not row["query"].strip() or not row["expected_policy_ids"]:
            raise ValueError(f"case {row['case_id']} must have a query and policy label")
        if row["review_status"] == "approved" and not row.get("evidence_reference"):
            raise ValueError(f"approved case {row['case_id']} needs evidence_reference")
    return rows


def goldset_readiness(path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    rows = load_goldset_candidates(path)
    statuses = Counter(str(row["review_status"]) for row in rows)
    axes = Counter(str(row["quality_axis"]) for row in rows)
    return {
        "dataset_version": "v1",
        "case_count": len(rows),
        "quality_axis_counts": dict(sorted(axes.items())),
        "review_status_counts": dict(sorted(statuses.items())),
        "approved_case_count": statuses["approved"],
        "ready_for_accuracy_reporting": statuses["needs_policy_review"] == 0
        and statuses["rejected"] == 0,
    }


if __name__ == "__main__":
    print(json.dumps(goldset_readiness(), ensure_ascii=False, indent=2))
