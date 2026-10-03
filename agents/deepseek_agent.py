"""
DeepSeek agent — direct API (https://api.deepseek.com/v1).

Does not support vision; use OpenRouter or Gemini for image questions.
"""

from __future__ import annotations

from typing import Optional

from .openai_compat import OpenAICompatibleAgent

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekAgent(OpenAICompatibleAgent):
    """Generic DeepSeek agent — model id passed at construction time."""

    supports_vision = False
    strip_think_blocks = True
    max_tokens = 4096

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            api_key,
            model=model,
            name=name or f"DeepSeek({model})",
            base_url=DEEPSEEK_BASE_URL,
            max_tokens=4096,
            strip_think_blocks=True,
            supports_vision=False,
        )


class DeepSeekV4FlashAgent(DeepSeekAgent):
    """Legacy alias for deepseek-chat."""

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="deepseek-chat", name="DeepSeekV4Flash")
