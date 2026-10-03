import sys
from pathlib import Path

import asyncio
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.gemini_agent import GeminiFlash25Agent


async def main() -> None:
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    key = cfg["agents"]["google_api_key"]
    agent = GeminiFlash25Agent(key)

    prompt = 'Return ONLY JSON: {"answer":"B","confidence":0.9,"reasoning":"test"}'

    t0 = time.monotonic()
    ans = await agent.ask(prompt, temperature=0.2)
    dt = time.monotonic() - t0

    print("latency_s:", round(dt, 3))
    print("agent:", ans.agent_name, "model:", ans.model_name)
    print("error:", ans.error)
    print("answer:", ans.answer)
    print("raw_prefix:", (ans.raw_response or "")[:300])


if __name__ == "__main__":
    asyncio.run(main())

