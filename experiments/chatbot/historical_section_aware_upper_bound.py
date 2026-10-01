"""Historical application-subset replay and oracle upper-bound estimate.

The historical baseline rows are copied from the stored scoped vector benchmark.
The "oracle section-aware upper bound" only asks whether a target section exists
in the already known policy section inventory. It is not a K=7 production result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.ai.tools.policy_chunk_search_tool import (
    SECTION_BY_SUBTYPE,
    infer_application_section_subtype,
)

BASELINE_PATH = Path(__file__).with_name("historical_application_scoped_baseline.json")
INVENTORY_PATH = Path(__file__).with_name("application_policy_section_inventory.json")


def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 2) if d else 0.0


def main() -> None:
    baseline: list[dict[str, Any]] = json.loads(
        BASELINE_PATH.read_text(encoding="utf-8")
    )
    inventory_rows: list[dict[str, Any]] = json.loads(
        INVENTORY_PATH.read_text(encoding="utf-8")
    )
    inventory = {
        int(row["policy_id"]): {chunk["section"] for chunk in row["chunks"]}
        for row in inventory_rows
    }

    rows: list[dict[str, Any]] = []
    for case in baseline:
        subtype = infer_application_section_subtype(case["query"])
        subtype_section = SECTION_BY_SUBTYPE.get(subtype or "")
        policy_id = int(case["expected_policy_ids"][0])
        policy_sections = inventory.get(policy_id, set())

        # Some historical cases allow more than one acceptable expected section.
        expected_sections = set(case["expected_sections"])
        target_exists = bool(expected_sections & policy_sections)
        subtype_target_matches = (
            subtype_section in expected_sections
            if subtype_section is not None
            else False
        )

        rows.append(
            {
                "case_id": case["case_id"],
                "baseline_hit": bool(case["section_hit"]),
                "predicted_subtype": subtype,
                "subtype_target_matches_expected": subtype_target_matches,
                "target_exists_in_policy_inventory": target_exists,
                "oracle_recoverable": (
                    not case["section_hit"]
                    and subtype_target_matches
                    and subtype_section in policy_sections
                ),
            }
        )

    baseline_hits = sum(row["baseline_hit"] for row in rows)
    recoverable = sum(row["oracle_recoverable"] for row in rows)
    subtype_matches = sum(row["subtype_target_matches_expected"] for row in rows)

    result = {
        "case_count": len(rows),
        "historical_baseline_section_hit_at_5_pct": _pct(
            baseline_hits, len(rows)
        ),
        "subtype_matches_expected_pct": _pct(subtype_matches, len(rows)),
        "oracle_recoverable_miss_count": recoverable,
        "oracle_section_aware_upper_bound_pct": _pct(
            baseline_hits + recoverable, len(rows)
        ),
        "oracle_upper_bound_delta_pp": round(
            _pct(baseline_hits + recoverable, len(rows))
            - _pct(baseline_hits, len(rows)),
            2,
        ),
        "warning": (
            "Upper bound only: target section existence does not prove it is "
            "inside the production K=7 candidate pool."
        ),
        "rows": rows,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
