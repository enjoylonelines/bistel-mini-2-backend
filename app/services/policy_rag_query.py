"""Conservative, reviewed wording aliases for policy-document retrieval."""

from __future__ import annotations

import re


_POLICY_QUERY_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"임신\s*준비\s*시술"),
        "난임치료 휴가 급여",
    ),
    (
        re.compile(r"신생아.*목돈|목돈.*신생아"),
        "출생아 첫만남 이용권",
    ),
)


def expand_policy_retrieval_query(query: str) -> str:
    """Append only reviewed paraphrases that have a precise source-policy term.

    The original query remains the audit value. This function changes only the
    vector query and leaves unrelated wording untouched.
    """
    normalized = " ".join(str(query or "").split())
    if not normalized:
        return normalized

    aliases = [
        alias
        for pattern, alias in _POLICY_QUERY_ALIASES
        if pattern.search(normalized)
    ]
    return " ".join([normalized, *aliases])
