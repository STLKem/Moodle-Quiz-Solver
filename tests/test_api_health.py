from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

from agents.orchestrator import build_agents


PROMPT = (
    "Return ONLY JSON in this format: "
    '{"answer":"pong","confidence":0.9,"reasoning":""}'
)


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cfg_path = root / "config.yaml"
    if not cfg_path.exists():
        print("config.yaml not found. Copy config.example.yaml → config.yaml")
        return 2
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    agent_cfg = cfg.get("agents", {}) or {}
    agents = build_agents(agent_cfg)

    if not agents:
        print("No agents enabled / no keys configured.")
        return 2

    timeout_s = float(agent_cfg.get("timeout_seconds", 25.0) or 25.0)
    temp = float(agent_cfg.get("temperature_low", 0.2) or 0.2)

    async def one(agent):
        t0 = time.monotonic()
        try:
            ans = await asyncio.wait_for(agent.ask(PROMPT, temp), timeout=timeout_s)
        except Exception as exc:
            dt = time.monotonic() - t0
            return (agent.name, getattr(agent, "model", "unknown"), False, dt, str(exc))
        dt = time.monotonic() - t0
        ok = bool(ans and not ans.error and str(ans.answer).strip())
        err = ans.error or ""
        return (agent.name, getattr(agent, "model", "unknown"), ok, dt, err)

    results = await asyncio.gather(*[one(a) for a in agents])

    print(f"Agents: {len(results)}  timeout={timeout_s}s  temp={temp}")
    bad = 0
    for name, model, ok, dt, err in results:
        status = "OK" if ok else "ERR"
        line = f"{status:3}  {dt:6.2f}s  {name}  ({model})"
        if err:
            line += f"  -> {err}"
        print(line)
        if not ok:
            bad += 1

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

