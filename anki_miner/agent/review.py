"""Versioned, agent-only semantic review contract."""

from __future__ import annotations

from typing import Any

REVIEW_SPEC_VERSION = "candidate_review_v1"
SELECT_REASON = "clear_supported_target"
REJECT_REASONS = frozenset(
    {
        "unsupported_sense",
        "ambiguous_context",
        "suspicious_text",
    }
)
REVIEW_REASON_CODES = frozenset({SELECT_REASON, *REJECT_REASONS})

REVIEW_INSTRUCTION = (
    "Review quality, not quantity; zero selections is valid. Select only when one prepared definition option "
    "clearly matches the target in the complete sentence. Reject ambiguity or suspicious Japanese. Never "
    "repair or replace source text. Use exactly one allowed reason code and review every returned candidate."
)


def review_contract() -> dict[str, Any]:
    return {
        "version": REVIEW_SPEC_VERSION,
        "instruction": REVIEW_INSTRUCTION,
        "decisions": ["select", "reject"],
        "select_reason": SELECT_REASON,
        "reject_reasons": sorted(REJECT_REASONS),
        "fields": ["candidate_id", "decision", "definition_option_id", "reason_code"],
        "optional_fields": ["rationale", "enrichments"],
    }
