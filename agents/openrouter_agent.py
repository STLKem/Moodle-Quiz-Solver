"""
OpenRouter agents — many models behind one API key.

Get a key at: https://openrouter.ai

Config example:
  openrouter_api_key: sk-or-v1-...
  openrouter_models:
    - model: deepseek/deepseek-r1:free
      enabled: true
"""

from __future__ import annotations

from .keys import is_valid_key
from .openai_compat import OpenAICompatibleAgent

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_EXTRA_HEADERS = {
    "HTTP-Referer": "https://github.com/STLKem",
    "X-Title": "Moodle Quiz Solver",
}


def _slugify(model: str) -> str:
    """Turn 'google/gemma-4:free' into 'OR_google_gemma_4_free'."""
    return "OR_" + model.replace("/", "_").replace("-", "_").replace(":", "_")


class OpenRouterAgent(OpenAICompatibleAgent):
    """
    Generic OpenRouter agent.
    Pass the model id from config — no code changes needed for new models.
    """

    supports_vision = True
    strip_think_blocks = True

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(
            api_key,
            model=model,
            name=_slugify(model),
            base_url=OPENROUTER_BASE_URL,
            default_headers=_EXTRA_HEADERS,
            strip_think_blocks=True,
            supports_vision=True,
        )


def build_openrouter_agents(
    api_key: str, models_config: list[dict]
) -> list[OpenRouterAgent]:
    """Build agents from a list of {model, enabled} entries."""
    agents: list[OpenRouterAgent] = []
    if not is_valid_key(api_key):
        return agents
    for entry in models_config:
        if entry.get("enabled", False) and entry.get("model"):
            agents.append(OpenRouterAgent(api_key, entry["model"]))
    return agents
