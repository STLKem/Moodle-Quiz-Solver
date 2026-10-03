"""
Shared OpenAI-compatible chat agent.

Cerebras, DeepSeek, OpenRouter, and GitHub Models all speak the same
chat.completions API — this module removes the duplicated retry/call logic.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from .base import AgentAnswer, BaseAgent

DEFAULT_RETRY_DELAYS = (0, 10, 30)
DEFAULT_MAX_TOKENS = 1024
_RETRYABLE = ("429", "rate", "limit", "overloaded", "503", "502", "quota")
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


class OpenAICompatibleAgent(BaseAgent):
    """Agent backed by any OpenAI-compatible chat completions endpoint."""

    supports_vision: bool = False
    strip_think_blocks: bool = False
    max_tokens: int = DEFAULT_MAX_TOKENS
    retry_delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        name: str,
        base_url: str,
        default_headers: Optional[dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        strip_think_blocks: Optional[bool] = None,
        supports_vision: Optional[bool] = None,
        retry_delays: Optional[tuple[int, ...]] = None,
    ) -> None:
        super().__init__(api_key)
        self.model = model
        self.name = name
        if max_tokens is not None:
            self.max_tokens = max_tokens
        if strip_think_blocks is not None:
            self.strip_think_blocks = strip_think_blocks
        if supports_vision is not None:
            self.supports_vision = supports_vision
        if retry_delays is not None:
            self.retry_delays = retry_delays
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers or {},
        )

    async def ask(self, prompt: str, temperature: float = 0.2) -> AgentAnswer:
        messages = [{"role": "user", "content": prompt}]
        return await self._chat(messages, temperature)

    async def ask_with_image(
        self, prompt: str, image_base64: str, temperature: float = 0.2
    ) -> AgentAnswer:
        if not self.supports_vision:
            return self._error_answer(
                temperature,
                f"{self.name} does not support vision input.",
            )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_base64}"
                        },
                    },
                ],
            }
        ]
        return await self._chat(messages, temperature)

    async def _chat(
        self, messages: list[dict[str, Any]], temperature: float
    ) -> AgentAnswer:
        last_exc: Exception | None = None
        for delay in self.retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=self.max_tokens,
                )
                raw = response.choices[0].message.content or ""
                if not raw:
                    raise ValueError(f"Empty response from {self.name} API")
                text = self._prepare_raw(raw)
                if not text:
                    raise ValueError("Empty response after stripping think blocks")
                return self._parse_answer_json(text, temperature)
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                if "empty response" in err:
                    continue
                if not any(token in err for token in _RETRYABLE):
                    break
        return self._error_answer(temperature, str(last_exc))

    def _prepare_raw(self, raw: str) -> str:
        if not self.strip_think_blocks:
            return raw
        cleaned = _THINK_RE.sub("", raw).strip()
        if cleaned:
            return cleaned
        # Some models emit an unclosed <think> block
        cleaned = _THINK_OPEN_RE.sub("", raw).strip()
        return cleaned or raw

    def _error_answer(self, temperature: float, error: str) -> AgentAnswer:
        return AgentAnswer(
            agent_name=self.name,
            model_name=self.model,
            answer="",
            confidence=0.0,
            reasoning="",
            temperature=temperature,
            raw_response="",
            error=error,
        )
