"""
Agent orchestrator.

Builds enabled agents from config, runs them in parallel with alternating
temperatures, and returns AgentAnswer objects for the voting engine.
"""

from __future__ import annotations

import asyncio

from .base import AgentAnswer, BaseAgent
from .registry import build_agents

__all__ = ["AgentOrchestrator", "build_agents"]


class AgentOrchestrator:
    """
    Runs all agents in parallel, each called `calls_per_agent` times
    with alternating temperatures. Returns all answers for voting.
    """

    def __init__(
        self,
        agents: list[BaseAgent],
        calls_per_agent: int = 2,
        temperature_low: float = 0.2,
        temperature_high: float = 0.7,
        timeout_seconds: float = 270.0,
    ) -> None:
        if not agents:
            raise ValueError(
                "No agents configured. Check your API keys in config.yaml."
            )
        self.agents = agents
        self.calls_per_agent = calls_per_agent
        self.temperature_low = temperature_low
        self.temperature_high = temperature_high
        self.timeout_seconds = timeout_seconds

    async def ask_all(self, prompt: str) -> list[AgentAnswer]:
        """Ask every agent in parallel; return all answers (including errors)."""
        tasks: list[asyncio.Task[AgentAnswer]] = []

        for agent in self.agents:
            for temp in self._temperatures():
                tasks.append(
                    asyncio.create_task(self._ask_with_timeout(agent, prompt, temp))
                )

        return list(await asyncio.gather(*tasks))

    async def _ask_with_timeout(
        self, agent: BaseAgent, prompt: str, temperature: float
    ) -> AgentAnswer:
        try:
            return await asyncio.wait_for(
                agent.ask(prompt, temperature),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return AgentAnswer(
                agent_name=agent.name,
                model_name=agent.model,
                answer="",
                confidence=0.0,
                reasoning="",
                temperature=temperature,
                raw_response="",
                error=f"Timeout after {self.timeout_seconds}s",
            )

    def _temperatures(self) -> list[float]:
        if self.calls_per_agent <= 1:
            return [self.temperature_low]
        temps: list[float] = []
        for i in range(self.calls_per_agent):
            temps.append(
                self.temperature_low if i % 2 == 0 else self.temperature_high
            )
        return temps
