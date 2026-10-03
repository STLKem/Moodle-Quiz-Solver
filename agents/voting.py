"""
Voting engine.

Aggregates answers from all agent calls and selects the winner by
majority vote. Tie-breaking uses the sum of confidence scores.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional  # noqa: F401 – used in function signatures

from .base import AgentAnswer


@dataclass
class VoteResult:
    """The outcome of the voting round for a single question."""
    winner: str | list[str] | dict[str, str]
    vote_count: int          # How many agents voted for the winner
    total_votes: int         # Total valid (non-error) votes cast
    confidence_sum: float    # Sum of confidence of all agents that agreed
    reasoning_samples: list[str] = field(default_factory=list)
    tiebreak_used: bool = False
    error_count: int = 0
    all_answers: list[AgentAnswer] = field(default_factory=list)

    @property
    def win_ratio(self) -> float:
        return self.vote_count / self.total_votes if self.total_votes else 0.0

    def summary(self) -> str:
        pct = f"{self.win_ratio * 100:.0f}%"
        tiebreak = " (tiebreak)" if self.tiebreak_used else ""
        errors = f", {self.error_count} errors" if self.error_count else ""
        return (
            f"Winner: {self.winner!r}  "
            f"({self.vote_count}/{self.total_votes} votes = {pct}){tiebreak}{errors}"
        )


def _normalise(answer: str | list[str] | dict[str, str]) -> str:
    """
    Convert an answer to a canonical hashable string for vote counting.
    Lists are sorted and joined; dicts are serialised with sorted keys.
    """
    if isinstance(answer, list):
        return json.dumps(sorted(str(x).strip().upper() for x in answer))
    if isinstance(answer, dict):
        return json.dumps(
            {str(k).strip().upper(): str(v).strip().upper() for k, v in sorted(answer.items())},
            ensure_ascii=False,
        )
    return str(answer).strip().upper()


def _denormalise(normalised: str, original_answers: list[AgentAnswer]) -> str | list[str] | dict[str, str]:
    """
    Recover the original typed answer from the first agent that gave it.
    """
    for ans in original_answers:
        if _normalise(ans.answer) == normalised:
            return ans.answer
    return normalised


def vote(
    answers: list[AgentAnswer],
    vision_answers: Optional[list[AgentAnswer]] = None,
    vision_weight: int = 3,
) -> VoteResult:
    """
    Count votes and return a VoteResult.

    If ``vision_answers`` is provided and contains successful responses,
    each vision answer is counted ``vision_weight`` times so vision models
    aren't diluted by text-only models that lack visual context.

    Algorithm:
    1. Discard answers with errors or empty answer fields.
    2. Normalise all answers to canonical strings for comparison.
    3. Count votes per candidate (vision answers count as vision_weight votes).
    4. Winner = candidate with the most votes.
    5. Tie → winner = candidate with highest confidence sum.
    """
    valid_text = [a for a in answers if not a.error and a.answer not in ("", [], {})]
    valid_vision: list[AgentAnswer] = []
    if vision_answers:
        valid_vision = [a for a in vision_answers if not a.error and a.answer not in ("", [], {})]

    all_valid = valid_text + valid_vision
    error_count = len(answers) + len(vision_answers or []) - len(all_valid)

    if not all_valid:
        return VoteResult(
            winner="",
            vote_count=0,
            total_votes=0,
            confidence_sum=0.0,
            error_count=error_count,
            all_answers=answers + (vision_answers or []),
        )

    vote_counts: dict[str, int] = defaultdict(int)
    confidence_sums: dict[str, float] = defaultdict(float)
    reasoning_map: dict[str, list[str]] = defaultdict(list)

    for a in valid_text:
        key = _normalise(a.answer)
        vote_counts[key] += 1
        confidence_sums[key] += a.confidence
        if a.reasoning:
            reasoning_map[key].append(f"[{a.agent_name} t={a.temperature}] {a.reasoning}")

    for a in valid_vision:
        key = _normalise(a.answer)
        vote_counts[key] += vision_weight
        confidence_sums[key] += a.confidence * vision_weight
        if a.reasoning:
            reasoning_map[key].append(f"[VISION:{a.agent_name}] {a.reasoning}")

    # Effective total vote count (for ratio display)
    total_votes = len(valid_text) + len(valid_vision) * vision_weight

    max_votes = max(vote_counts.values())
    top_candidates = [k for k, v in vote_counts.items() if v == max_votes]

    tiebreak_used = len(top_candidates) > 1
    winner_key = max(top_candidates, key=lambda k: confidence_sums[k])
    winner_answer = _denormalise(winner_key, all_valid)

    return VoteResult(
        winner=winner_answer,
        vote_count=vote_counts[winner_key],
        total_votes=total_votes,
        confidence_sum=confidence_sums[winner_key],
        reasoning_samples=reasoning_map[winner_key][:3],
        tiebreak_used=tiebreak_used,
        error_count=error_count,
        all_answers=answers + (vision_answers or []),
    )


def vote_essay(answers: list[AgentAnswer]) -> VoteResult:
    """
    Special voting for essay questions: since essays are unique text,
    we can't do majority vote. Instead, pick the answer with the
    highest confidence from non-error responses.
    """
    valid = [a for a in answers if not a.error and a.answer not in ("", [], {})]
    error_count = len(answers) - len(valid)

    if not valid:
        return VoteResult(
            winner="",
            vote_count=0,
            total_votes=0,
            confidence_sum=0.0,
            error_count=error_count,
            all_answers=answers,
        )

    best = max(valid, key=lambda a: a.confidence)
    return VoteResult(
        winner=best.answer,
        vote_count=1,
        total_votes=len(valid),
        confidence_sum=best.confidence,
        reasoning_samples=[best.reasoning] if best.reasoning else [],
        tiebreak_used=False,
        error_count=error_count,
        all_answers=answers,
    )
