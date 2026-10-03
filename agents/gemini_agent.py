"""
Google Gemini agents (2.5 Flash and 2.0 Flash).

Both share one Google AI Studio API key.
Uses the official google-genai SDK.
"""

from __future__ import annotations

import asyncio
import base64
import re

from google import genai
from google.genai import types as genai_types

from .base import AgentAnswer, BaseAgent

_RETRY_DELAYS = (0, 5, 15, 30)
_RETRYABLE = ("429", "quota", "rate", "resource_exhausted")
_RETRY_IN_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)


class GeminiAgent(BaseAgent):
    """Generic Gemini agent — model name is set at construction time."""

    supports_vision: bool = False

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", name: str | None = None) -> None:
        super().__init__(api_key)
        self.model = model
        self.name = name or f"Gemini({model})"
        self._client = genai.Client(api_key=api_key)

    async def ask(self, prompt: str, temperature: float = 0.2) -> AgentAnswer:
        return await self._generate(prompt, temperature)

    async def ask_with_image(
        self, prompt: str, image_base64: str, temperature: float = 0.2
    ) -> AgentAnswer:
        if not self.supports_vision:
            return self._error(temperature, f"{self.name} does not support vision.")
        image_bytes = base64.b64decode(image_base64)
        image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png")
        return await self._generate([image_part, prompt], temperature)

    async def _generate(self, contents, temperature: float) -> AgentAnswer:
        last_exc: Exception | None = None
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                config = genai_types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=1024,
                )
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                raw = response.text or ""
                if not raw:
                    raise ValueError("Empty response from Gemini API")
                return self._parse_answer_json(raw, temperature)
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                if "empty response" in err:
                    continue
                match = _RETRY_IN_RE.search(err)
                if match:
                    await asyncio.sleep(float(match.group(1)) + 0.5)
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


class GeminiFlash25Agent(GeminiAgent):
    """Gemini 2.5 Flash — supports images."""

    supports_vision = True

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="gemini-2.5-flash", name="Gemini2.5Flash")


class GeminiFlash20Agent(GeminiAgent):
    """Gemini 2.0 Flash — fast and stable."""

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="gemini-2.0-flash", name="Gemini2.0Flash")
