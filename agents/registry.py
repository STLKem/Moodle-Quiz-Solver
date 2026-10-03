"""
Central model registry.

Single source of truth for built-in agents, legacy toggles, and factory helpers.
"""

from __future__ import annotations

from typing import Type

from .base import BaseAgent
from .cerebras_agent import CerebrasLlama31Agent, CerebrasQwen3Agent
from .deepseek_agent import DeepSeekAgent
from .gemini_agent import GeminiFlash20Agent, GeminiFlash25Agent
from .github_agent import GitHubDeepSeekR1Agent, GitHubGPT4oAgent, GitHubO4MiniAgent
from .groq_agent import GroqLlama33Agent, GroqLlama4ScoutAgent, GroqQwen3Agent
from .keys import get_key, is_valid_key
from .openrouter_agent import OpenRouterAgent, build_openrouter_agents

# toggle_id → (label, agent class | None, api key field, default_enabled)
# class=None means legacy alias handled separately (OpenRouter / DeepSeek)
AGENT_DEFS: dict[str, tuple[str, Type[BaseAgent] | None, str, bool]] = {
    "gemini_flash_25": ("Gemini 2.5 Flash", GeminiFlash25Agent, "google_api_key", True),
    "gemini_flash_20": ("Gemini 2.0 Flash", GeminiFlash20Agent, "google_api_key", False),
    "groq_llama33_70b": ("Groq Llama 3.3 70B", GroqLlama33Agent, "groq_api_key", True),
    "groq_llama4_scout": ("Groq Llama 4 Scout", GroqLlama4ScoutAgent, "groq_api_key", False),
    "groq_qwen3_32b": ("Groq Qwen3 32B", GroqQwen3Agent, "groq_api_key", True),
    "cerebras_llama31_8b": ("Cerebras Llama 3.1 8B", CerebrasLlama31Agent, "cerebras_api_key", True),
    "cerebras_qwen3_235b": ("Cerebras Qwen3 235B", CerebrasQwen3Agent, "cerebras_api_key", True),
    "github_gpt4o": ("GitHub GPT-4o", GitHubGPT4oAgent, "github_api_key", False),
    "github_o4mini": ("GitHub o4-mini", GitHubO4MiniAgent, "github_api_key", False),
    "github_deepseek_r1": ("GitHub DeepSeek-R1", GitHubDeepSeekR1Agent, "github_api_key", False),
    "deepseek_v4_flash": ("DeepSeek V4 Flash (legacy)", None, "deepseek_api_key", False),
    "openrouter_gemini25pro": ("OpenRouter Gemini 2.5 Pro (legacy)", None, "openrouter_api_key", False),
    "openrouter_deepseek_r1": ("OpenRouter DeepSeek R1 (legacy)", None, "openrouter_api_key", False),
    "openrouter_llama4_mav": ("OpenRouter Llama 4 Maverick (legacy)", None, "openrouter_api_key", False),
}

BUILTIN_AGENTS: dict[str, tuple[Type[BaseAgent], str]] = {
    key: (cls, key_field)
    for key, (_label, cls, key_field, _default) in AGENT_DEFS.items()
    if cls is not None
}

DEFAULT_ENABLED: dict[str, bool] = {
    key: default for key, (_label, _cls, _field, default) in AGENT_DEFS.items()
}

LEGACY_OPENROUTER: dict[str, str] = {
    "openrouter_gemini25pro": "google/gemini-2.5-pro-exp-03-25:free",
    "openrouter_deepseek_r1": "deepseek/deepseek-r1:free",
    "openrouter_llama4_mav": "meta-llama/llama-4-maverick:free",
}

PROVIDER_KEYS = (
    "google_api_key",
    "groq_api_key",
    "cerebras_api_key",
    "deepseek_api_key",
    "openrouter_api_key",
    "github_api_key",
)


def catalog_entries(config: dict) -> list[dict]:
    """Full toggle catalog for the GUI (builtin + dynamic model lists)."""
    enabled = (config.get("enabled") or {}) if isinstance(config, dict) else {}
    rows: list[dict] = []

    for key, (label, cls, key_field, default) in AGENT_DEFS.items():
        rows.append(
            {
                "id": key,
                "label": label,
                "provider_key": key_field,
                "enabled": bool(enabled.get(key, default)),
                "kind": "builtin" if cls is not None else "legacy",
                "group": "agents",
            }
        )

    for entry in config.get("deepseek_models") or []:
        model = str(entry.get("model") or "").strip()
        if not model:
            continue
        rows.append(
            {
                "id": f"deepseek::{model}",
                "label": f"DeepSeek · {model}",
                "provider_key": "deepseek_api_key",
                "enabled": bool(entry.get("enabled", False)),
                "kind": "deepseek",
                "group": "deepseek",
                "model": model,
            }
        )

    for entry in config.get("openrouter_models") or []:
        model = str(entry.get("model") or "").strip()
        if not model:
            continue
        rows.append(
            {
                "id": f"openrouter::{model}",
                "label": f"OpenRouter · {model}",
                "provider_key": "openrouter_api_key",
                "enabled": bool(entry.get("enabled", False)),
                "kind": "openrouter",
                "group": "openrouter",
                "model": model,
            }
        )

    return rows


def build_agents(config: dict) -> list[BaseAgent]:
    """Instantiate every enabled agent from the `agents` config section."""
    agents: list[BaseAgent] = []
    enabled: dict = config.get("enabled", {}) or {}

    for toggle, (cls, key_field) in BUILTIN_AGENTS.items():
        if not enabled.get(toggle, DEFAULT_ENABLED.get(toggle, False)):
            continue
        api_key = get_key(config, key_field)
        if is_valid_key(api_key):
            agents.append(cls(api_key))

    deepseek_key = get_key(config, "deepseek_api_key")
    deepseek_models: list[dict] = config.get("deepseek_models") or []
    if deepseek_models and is_valid_key(deepseek_key):
        for entry in deepseek_models:
            if entry.get("enabled", False) and entry.get("model"):
                agents.append(DeepSeekAgent(deepseek_key, str(entry["model"])))
    elif enabled.get("deepseek_v4_flash", False) and is_valid_key(deepseek_key):
        agents.append(DeepSeekAgent(deepseek_key, "deepseek-chat", name="DeepSeekV4Flash"))

    openrouter_key = get_key(config, "openrouter_api_key")
    if is_valid_key(openrouter_key):
        agents.extend(
            build_openrouter_agents(openrouter_key, config.get("openrouter_models") or [])
        )
        for toggle, model_id in LEGACY_OPENROUTER.items():
            if enabled.get(toggle, False):
                agents.append(OpenRouterAgent(openrouter_key, model_id))

    return agents


def build_single_agent(agent_cfg: dict, model_name: str) -> BaseAgent:
    """Build one agent by config toggle name or provider model id."""
    deepseek_key = get_key(agent_cfg, "deepseek_api_key")
    openrouter_key = get_key(agent_cfg, "openrouter_api_key")

    for entry in agent_cfg.get("deepseek_models") or []:
        if entry.get("model") == model_name:
            if not is_valid_key(deepseek_key):
                raise ValueError(f"DeepSeek API key not configured for '{model_name}'")
            return DeepSeekAgent(deepseek_key, model_name)

    for entry in agent_cfg.get("openrouter_models") or []:
        if entry.get("model") == model_name:
            if not is_valid_key(openrouter_key):
                raise ValueError(f"OpenRouter API key not configured for '{model_name}'")
            return OpenRouterAgent(openrouter_key, model_name)

    normalized = model_name.replace("-", "_")
    if normalized in BUILTIN_AGENTS:
        cls, key_field = BUILTIN_AGENTS[normalized]
        api_key = get_key(agent_cfg, key_field)
        if not is_valid_key(api_key):
            raise ValueError(f"API key '{key_field}' not configured for '{model_name}'")
        return cls(api_key)

    if normalized in LEGACY_OPENROUTER:
        if not is_valid_key(openrouter_key):
            raise ValueError(f"OpenRouter API key not configured for '{model_name}'")
        return OpenRouterAgent(openrouter_key, LEGACY_OPENROUTER[normalized])

    if model_name.startswith("deepseek-") or model_name.startswith("deepseek_"):
        if not is_valid_key(deepseek_key):
            raise ValueError(f"DeepSeek API key not configured for '{model_name}'")
        return DeepSeekAgent(deepseek_key, model_name.replace("_", "-"))

    available = list_available_models(agent_cfg)
    raise ValueError(f"Unknown model '{model_name}'. Available: {', '.join(available)}")


def list_available_models(agent_cfg: dict) -> list[str]:
    models = list(AGENT_DEFS.keys())
    for entry in agent_cfg.get("deepseek_models") or []:
        if entry.get("model"):
            models.append(str(entry["model"]))
    for entry in agent_cfg.get("openrouter_models") or []:
        if entry.get("model"):
            models.append(str(entry["model"]))
    return sorted(set(models))
