"""
Cerebras agents — OpenAI-compatible endpoint.

Docs: https://cloud.cerebras.ai/
"""

from __future__ import annotations

from .openai_compat import OpenAICompatibleAgent

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"


class CerebrasAgent(OpenAICompatibleAgent):
    """Generic Cerebras agent — pass model and display name at construction."""

    def __init__(self, api_key: str, model: str, name: str) -> None:
        super().__init__(
            api_key,
            model=model,
            name=name,
            base_url=CEREBRAS_BASE_URL,
        )


class CerebrasLlama31Agent(CerebrasAgent):
    """Llama 3.1 8B on Cerebras."""

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="llama3.1-8b", name="CerebrasLlama31")


class CerebrasQwen3Agent(CerebrasAgent):
    """Qwen3 235B Instruct on Cerebras."""

    def __init__(self, api_key: str) -> None:
        super().__init__(
            api_key,
            model="qwen-3-235b-a22b-instruct-2507",
            name="CerebrasQwen3",
        )


# Backward-compatible alias (old misspelled classname)
CerebasQwen3Agent = CerebrasQwen3Agent
