"""
GitHub Models agents — GPT-4o, o4-mini, DeepSeek-R1 via Azure AI inference.

Create a classic Personal Access Token at:
  https://github.com/settings/tokens
Student Developer Pack gives higher rate limits:
  https://education.github.com/pack
"""

from __future__ import annotations

from .openai_compat import OpenAICompatibleAgent

GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"


class GitHubModelsAgent(OpenAICompatibleAgent):
    """Generic GitHub Models agent."""

    strip_think_blocks = True

    def __init__(self, api_key: str, model: str, name: str) -> None:
        super().__init__(
            api_key,
            model=model,
            name=name,
            base_url=GITHUB_MODELS_BASE_URL,
            strip_think_blocks=True,
            retry_delays=(0,),  # fail fast; GitHub errors are usually auth/quota
        )


class GitHubGPT4oAgent(GitHubModelsAgent):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="gpt-4o", name="GH_GPT4o")


class GitHubO4MiniAgent(GitHubModelsAgent):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="o4-mini", name="GH_o4mini")


class GitHubDeepSeekR1Agent(GitHubModelsAgent):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key, model="DeepSeek-R1", name="GH_DeepSeekR1")
