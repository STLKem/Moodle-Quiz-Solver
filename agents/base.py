"""
Base agent interface.

Every AI provider implements BaseAgent so the orchestrator can call
them uniformly and parse a shared JSON answer format.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentAnswer:
    """Structured answer returned by every agent."""

    agent_name: str
    model_name: str
    # MCQ / True-False: "B" or "True"
    # Multi-select: ["A", "C"]
    # Short / essay: plain text
    # Matching: {"Capital of France": "Paris"}
    answer: str | list[str] | dict[str, str]
    confidence: float  # 0.0 – 1.0
    reasoning: str
    temperature: float
    raw_response: str
    error: Optional[str] = None


class BaseAgent(ABC):
    """Abstract base class for all AI agents."""

    name: str = "BaseAgent"
    model: str = "unknown"
    supports_vision: bool = False

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    @abstractmethod
    async def ask(self, prompt: str, temperature: float) -> AgentAnswer:
        """
        Send a prompt and return a structured AgentAnswer.
        Must never raise — set the error field on failure instead.
        """

    def _parse_answer_json(self, raw: str, temperature: float) -> AgentAnswer:
        """
        Extract {"answer", "confidence", "reasoning"} from the model output.

        Tolerates thinking blocks (<think>...) and single-quoted JSON.
        """
        text = raw.strip()
        if not text:
            return AgentAnswer(
                agent_name=self.name,
                model_name=self.model,
                answer="",
                confidence=0.0,
                reasoning="",
                temperature=temperature,
                raw_response=raw,
                error="Empty response from API — nothing to parse",
            )

        for segment in re.split(r"<think>", text):
            seg = segment.strip()
            if not seg:
                continue

            try:
                return self._build_answer(json.loads(seg), raw, temperature)
            except json.JSONDecodeError:
                pass

            idx = seg.find("{")
            if idx < 0:
                continue

            candidate = seg[idx:]
            depth = 0
            end = 0
            for i, ch in enumerate(candidate):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            if end <= 0:
                continue

            js = candidate[:end]
            try:
                return self._build_answer(json.loads(js), raw, temperature)
            except json.JSONDecodeError:
                pass

            def _sq_to_dq(match: re.Match[str]) -> str:
                return '"' + match.group(1).replace('"', '\\"') + '"'

            try:
                converted = re.sub(r"'([^']*)'", _sq_to_dq, js)
                return self._build_answer(json.loads(converted), raw, temperature)
            except json.JSONDecodeError:
                pass

        return AgentAnswer(
            agent_name=self.name,
            model_name=self.model,
            answer="",
            confidence=0.0,
            reasoning="",
            temperature=temperature,
            raw_response=raw,
            error=f"Could not parse JSON from response: {text[:200]}",
        )

    def _build_answer(
        self, data: dict, raw: str, temperature: float
    ) -> AgentAnswer:
        answer = self._coerce_answer(data.get("answer", ""))
        confidence = float(data.get("confidence", 0.5))
        reasoning = str(data.get("reasoning", ""))
        return AgentAnswer(
            agent_name=self.name,
            model_name=self.model,
            answer=answer,
            confidence=min(max(confidence, 0.0), 1.0),
            reasoning=reasoning,
            temperature=temperature,
            raw_response=raw,
        )

    @staticmethod
    def _coerce_answer(answer: object) -> str | list[str] | dict[str, str]:
        """Normalize dashes and coerce JSON-encoded list/dict strings."""

        def _dash_normalize(value: str) -> str:
            return (
                value.replace("\u2013", "-")
                .replace("\u2014", "-")
                .replace("\u2212", "-")
            )

        if isinstance(answer, str):
            text = _dash_normalize(answer).strip()
            if (text.startswith("{") and text.endswith("}")) or (
                text.startswith("[") and text.endswith("]")
            ):
                try:
                    answer = json.loads(text)
                except Exception:
                    return text

        if isinstance(answer, list):
            return [_dash_normalize(str(item)) for item in answer]

        if isinstance(answer, dict):
            return {
                _dash_normalize(str(key)): _dash_normalize(str(value))
                for key, value in answer.items()
            }

        return _dash_normalize(str(answer))
