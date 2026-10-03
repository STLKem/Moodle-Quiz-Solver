"""
Quick connectivity test for all AI agents.
Run: python tests/test_agents.py
"""

import asyncio
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

TEST_PROMPT = """\
You are an exam assistant. Answer the following question.
CRITICAL: Respond with ONLY a valid JSON object, no extra text.

Question type: Multiple choice (exactly ONE correct answer)

QUESTION:
What is the capital of France?

OPTIONS:
  A. Berlin
  B. Madrid
  C. Paris
  D. Rome

{"answer": "C", "confidence": 0.99, "reasoning": "Paris is the capital of France."}
"""

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config.yaml"

if not CFG_PATH.exists():
    console.print("[red]config.yaml not found. Copy config.example.yaml → config.yaml[/red]")
    raise SystemExit(1)

with open(CFG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

agent_cfg = cfg.get("agents", {})


async def test_all() -> None:
    from agents.orchestrator import build_agents

    agents = build_agents(agent_cfg)
    if not agents:
        console.print("[red]No agents built — check API keys in config.yaml[/red]")
        raise SystemExit(1)

    console.print("\n[bold cyan]Testing all agents with a sample question...[/bold cyan]\n")

    results = await asyncio.gather(
        *[agent.ask(TEST_PROMPT, temperature=0.2) for agent in agents]
    )

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("Model", style="dim")
    table.add_column("Answer", style="white")
    table.add_column("Confidence", justify="right", style="yellow")
    table.add_column("Status", justify="center")

    all_ok = True
    for ans in results:
        if ans.error:
            status = "[red]FAIL[/red]"
            answer_text = f"[red]{ans.error[:60]}[/red]"
            all_ok = False
        elif ans.answer == "":
            status = "[yellow]WARN[/yellow]"
            answer_text = "[yellow]Empty answer (JSON parse issue?)[/yellow]"
            answer_text += f"\n[dim]{ans.raw_response[:80]}[/dim]"
            all_ok = False
        else:
            correct = str(ans.answer).strip().upper() == "C"
            status = "[green]OK[/green]" if correct else "[yellow]OK?[/yellow]"
            answer_text = str(ans.answer)
            if not correct:
                all_ok = False

        table.add_row(
            ans.agent_name,
            ans.model_name,
            answer_text,
            f"{ans.confidence:.2f}" if not ans.error else "-",
            status,
        )

    console.print(table)
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(test_all())
