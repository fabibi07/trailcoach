"""Prompt construction for the AI Coach."""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "You are TrailCoach, an expert running and trail-running coach. "
    "You answer the athlete's questions using only the provided Athlete State summary. "
    "You do NOT have access to raw FIT files, per-sample streams or GPS traces. "
    "Keep answers concise, actionable and grounded in the data. "
    "If the data is missing or the question is outside your scope, "
    "say so and suggest what to record."
)


def build_prompt(question: str, context: dict[str, Any]) -> str:
    """Build a single-turn prompt from the Athlete State context and the user's question."""
    context_json = json.dumps(context, indent=2, default=str, ensure_ascii=False)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Athlete State as of {context.get('as_of')}:\n"
        f"{context_json}\n\n"
        f"Question: {question}\n\n"
        "Answer:"
    )
