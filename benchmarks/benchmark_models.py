"""
Model benchmark runner.

Runs a deterministic dataset of questions against each enabled agent,
measures correctness, reliability (no errors/timeouts), and latency.

Usage:
  python main.py benchmark-models
  python main.py benchmark-models --apply --keep 2
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from rich.console import Console
from rich.table import Table
from rich import box

from agents.base import AgentAnswer, BaseAgent
from agents.orchestrator import build_agents


console = Console()


DATASET_PATH = Path("benchmarks/dataset_v1.json")


@dataclass
class BenchItem:
    id: str
    type: str
    prompt: str
    expected: Any


def _load_dataset(path: Path = DATASET_PATH, *, n: int = 40) -> list[BenchItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for raw in data.get("items", []):
        items.append(
            BenchItem(
                id=raw["id"],
                type=raw["type"],
                prompt=raw["prompt"],
                expected=raw["expected"],
            )
        )
    if len(items) < n:
        console.print(f"[yellow]Warning:[/yellow] dataset size is {len(items)} (< {n})")
    if len(items) > n:
        console.print(f"[dim]Dataset has {len(items)} items; using first {n} for speed.[/dim]")
        items = items[:n]
    return items


def _norm_str(s: str) -> str:
    s = s.strip()
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .,:;!?\"'")
    return s


def _is_correct(item: BenchItem, answer: Any) -> bool:
    exp = item.expected

    # Dict answers (cloze/matching)
    if isinstance(exp, dict):
        if not isinstance(answer, dict):
            return False
        exp_norm = {str(k).strip(): str(v).strip() for k, v in exp.items()}
        ans_norm = {str(k).strip(): str(v).strip() for k, v in answer.items()}
        # allow minor case differences in values
        exp_norm = {k: _norm_str(v) for k, v in exp_norm.items()}
        ans_norm = {k: _norm_str(v) for k, v in ans_norm.items()}
        return exp_norm == ans_norm

    # String answers
    if answer is None:
        return False

    if item.type in ("mcq",):
        a = str(answer).strip().upper()
        e = str(exp).strip().upper()
        return a == e

    if item.type in ("true_false",):
        a = _norm_str(str(answer))
        e = _norm_str(str(exp))
        # allow yes/no synonyms to reduce random failures
        a = {"t": "true", "f": "false", "yes": "true", "no": "false"}.get(a, a)
        return a == e.lower()

    # short answers: case-insensitive, punctuation-insensitive
    a = _norm_str(str(answer))
    e = _norm_str(str(exp))
    if e == "ohm":
        # accept Ω symbol too
        return a in ("ohm", "ω", "Ω".lower())
    return a == e


@dataclass
class AgentStats:
    agent_name: str
    model_name: str
    total: int = 0
    ok: int = 0
    correct: int = 0
    avg_latency_s: float = 0.0

    @property
    def reliability(self) -> float:
        return self.ok / self.total if self.total else 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def score(self) -> float:
        # Weighted: correctness most important, then reliability, then latency penalty.
        # Latency penalty saturates after ~3s average.
        lat_pen = min(self.avg_latency_s / 3.0, 1.0) * 0.10
        return (self.accuracy * 0.70) + (self.reliability * 0.30) - lat_pen


async def _ask_one(agent: BaseAgent, prompt: str, temperature: float, timeout_s: float) -> tuple[AgentAnswer, float]:
    t0 = time.monotonic()
    try:
        ans = await asyncio.wait_for(agent.ask(prompt, temperature), timeout=timeout_s)
    except asyncio.TimeoutError:
        ans = AgentAnswer(
            agent_name=agent.name,
            model_name=agent.model,
            answer="",
            confidence=0.0,
            reasoning="",
            temperature=temperature,
            raw_response="",
            error=f"Timeout after {timeout_s}s",
        )
    latency = time.monotonic() - t0
    return ans, latency


async def run_benchmark(
    cfg: dict,
    dataset_path: Path = DATASET_PATH,
    *,
    temperature: float = 0.2,
    timeout_s: float = 25.0,
    n: int = 40,
) -> tuple[list[AgentStats], dict[str, Any]]:
    agents_cfg = cfg.get("agents", {})
    agents = build_agents(agents_cfg)
    items = _load_dataset(dataset_path, n=n)

    per_agent_details: dict[str, list[dict[str, Any]]] = {}
    stats: list[AgentStats] = []

    for agent in agents:
        st = AgentStats(agent_name=agent.name, model_name=agent.model)
        total_latency = 0.0
        details = []

        console.print(f"[dim]Benchmarking {agent.name} ({agent.model})...[/dim]")
        consecutive_failures = 0
        for item in items:
            ans, lat = await _ask_one(agent, item.prompt, temperature, timeout_s)
            st.total += 1
            total_latency += lat
            ok = not ans.error and ans.answer not in ("", [], {})
            if ok:
                st.ok += 1
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            correct = ok and _is_correct(item, ans.answer)
            if correct:
                st.correct += 1
            details.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "expected": item.expected,
                    "got": ans.answer,
                    "error": ans.error,
                    "latency_s": round(lat, 3),
                    "correct": bool(correct),
                }
            )

            # Early stop: if the model is clearly failing (timeouts/ERR) and will waste time.
            if st.total >= 6:
                ok_rate = st.ok / st.total
                if consecutive_failures >= 5 or ok_rate < 0.15:
                    remaining = len(items) - st.total
                    if remaining > 0:
                        console.print(
                            f"[yellow]Early stop[/yellow] {agent.name} — too many failures "
                            f"(ok_rate={ok_rate:.0%}, consecutive_failures={consecutive_failures}). "
                            f"Skipping remaining {remaining} items."
                        )
                        for _ in range(remaining):
                            st.total += 1
                            details.append(
                                {
                                    "id": "(skipped)",
                                    "type": "",
                                    "expected": "",
                                    "got": "",
                                    "error": "skipped_due_to_failures",
                                    "latency_s": 0.0,
                                    "correct": False,
                                }
                            )
                    break

        st.avg_latency_s = total_latency / st.total if st.total else 0.0
        stats.append(st)
        per_agent_details[f"{agent.name}::{agent.model}"] = details

    stats.sort(key=lambda s: s.score, reverse=True)
    report = {
        "dataset": str(dataset_path),
        "temperature": temperature,
        "timeout_s": timeout_s,
        "results": [
            {
                "agent": s.agent_name,
                "model": s.model_name,
                "total": s.total,
                "ok": s.ok,
                "correct": s.correct,
                "accuracy": round(s.accuracy, 3),
                "reliability": round(s.reliability, 3),
                "avg_latency_s": round(s.avg_latency_s, 3),
                "score": round(s.score, 3),
            }
            for s in stats
        ],
        "details": per_agent_details,
    }
    return stats, report


def render_table(stats: list[AgentStats]) -> None:
    table = Table(title="Model Benchmark Results", box=box.SIMPLE, show_header=True)
    table.add_column("Rank", style="cyan", no_wrap=True)
    table.add_column("Agent", style="white")
    table.add_column("Model", style="dim")
    table.add_column("Correct/40", justify="right")
    table.add_column("OK/40", justify="right")
    table.add_column("Acc", justify="right")
    table.add_column("Rel", justify="right")
    table.add_column("Avg s", justify="right")
    table.add_column("Score", justify="right")

    for i, s in enumerate(stats, 1):
        table.add_row(
            str(i),
            s.agent_name,
            s.model_name,
            f"{s.correct}/40",
            f"{s.ok}/40",
            f"{s.accuracy*100:.0f}%",
            f"{s.reliability*100:.0f}%",
            f"{s.avg_latency_s:.2f}",
            f"{s.score:.3f}",
        )
    console.print(table)


def apply_best_models(cfg: dict, stats: list[AgentStats], keep: int = 2) -> dict:
    """
    Disable all agents except top `keep` by score.
    Also sets calls_per_agent = 1 for speed (1 call/question).
    """
    enabled = cfg.setdefault("agents", {}).setdefault("enabled", {})
    winner_models = {s.model_name for s in stats[:keep]}

    # Map model identifiers to config enabled flags.
    # Using model strings is more stable than agent.name (Gemini uses dynamic names).
    model_to_flag = {
        # Google AI Studio
        "gemini-2.5-flash": "gemini_flash_25",
        "gemini-2.0-flash": "gemini_flash_20",
        # Groq
        "llama-3.3-70b-versatile": "groq_llama33_70b",
        "meta-llama/llama-4-scout-17b-16e-instruct": "groq_llama4_scout",
        "qwen/qwen3-32b": "groq_qwen3_32b",
        # Cerebras
        "llama3.1-8b": "cerebras_llama31_8b",
        "qwen-3-235b-a22b-instruct-2507": "cerebras_qwen3_235b",
        # OpenRouter
        "google/gemini-2.5-pro-exp-03-25:free": "openrouter_gemini25pro",
        "deepseek/deepseek-r1:free": "openrouter_deepseek_r1",
        "meta-llama/llama-4-maverick:free": "openrouter_llama4_mav",
        # GitHub Models
        "gpt-4o": "github_gpt4o",
        "o4-mini": "github_o4mini",
        "DeepSeek-R1": "github_deepseek_r1",
    }

    # Disable everything, then enable winners.
    for flag in model_to_flag.values():
        enabled[flag] = False
    for model, flag in model_to_flag.items():
        if model in winner_models:
            enabled[flag] = True

    # speed settings
    cfg["agents"]["calls_per_agent"] = 1
    cfg["agents"]["timeout_seconds"] = min(float(cfg["agents"].get("timeout_seconds", 45)), 25.0)
    return cfg


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")


async def main(
    config_path: Path = Path("config.yaml"),
    *,
    apply: bool = False,
    keep: int = 2,
    out: Path = Path("benchmarks/benchmark_report.json"),
    timeout_s: float = 45.0,
    n: int = 40,
) -> None:
    cfg = load_config(config_path)
    stats, report = await run_benchmark(cfg, timeout_s=timeout_s, n=n)
    render_table(stats)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]Saved report: {out}[/dim]")

    if apply:
        new_cfg = apply_best_models(cfg, stats, keep=keep)
        save_config(config_path, new_cfg)
        console.print(f"[green]Updated {config_path}:[/green] kept top {keep} models, calls_per_agent=1")


if __name__ == "__main__":
    asyncio.run(main())

