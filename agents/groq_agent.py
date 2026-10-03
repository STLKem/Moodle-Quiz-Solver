"""
Groq agents — Llama 3.3, Llama 4 Scout, Qwen3 32B.

All models share one Groq API key.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from groq import AsyncGroq

from .base import AgentAnswer, BaseAgent

_RETRY_DELAYS = (0, 5, 15, 30)
_RETRYABLE = ("429", "rate", "limit", "overloaded", "503", "502")


class GroqAgent(BaseAgent):
    """Generic Groq agent — model name is set at construction time."""

    supports_vision: bool = False

    def __init__(self, api_key: str, model: str, name: str) -> None:
        super().__init__(api_key)
        self.model = model
        self.name = name
        self._client = AsyncGroq(api_key=api_key)

    async def ask(self, prompt: str, temperature: float = 0.2) -> AgentAnswer:
        messages = [{"role": "user", "content": prompt}]
        return await self._chat(messages, temperature)

    async def ask_with_image(
        self, prompt: str, image_base64: str, temperature: float = 0.2
    ) -> AgentAnswer:
        if not self.supports_vision:
            return self._error(temperature, f"{self.name} does not support vision.")
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

    async def _chat(self, messages: list, temperature: float) -> AgentAnswer:
        last_exc: Exception | None = None
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=1024,
                )
                raw = response.choices[0].message.content or ""
                if not raw:
                    raise ValueError("Empty response from Groq API")
                return self._parse_answer_json(raw, temperature)
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                if "empty response" in err:
                    continue
                if not any(token in err for token in _RETRYABLE):
                    break
        return self._error(temperature, str(last_exc))

    def _error(self, temperature: float, error: str) -> AgentAnswer:
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


class GroqLlama33Agent(GroqAgent):
    """Llama 3.3 70B — reliable large model."""

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="llama-3.3-70b-versatile", name="GroqLlama33")


class GroqLlama4ScoutAgent(GroqAgent):
    """Llama 4 Scout 17B — multimodal (image input supported)."""

    supports_vision = True

    def __init__(self, api_key: str) -> None:
        super().__init__(
            api_key,
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            name="GroqLlama4Scout",
        )


class GroqQwen3Agent(GroqAgent):
    """Qwen3 32B — strong reasoning model."""

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="qwen/qwen3-32b", name="GroqQwen3")
