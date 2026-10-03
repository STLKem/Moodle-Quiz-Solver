"""
Application entry point.

- Double-click / no args  → native desktop control panel
- With a CLI command      → Typer CLI (solve, watch-attempts, …)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLI_COMMANDS = {
    "solve",
    "list-quizzes",
    "benchmark-models",
    "remote-bots",
    "watch-attempts",
    "list-chats",
    "chat-bot",
    "mark-attempt",
    "gui",
    "desktop",
    "setup",
}


def _prepare_workdir() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def main() -> None:
    root = _prepare_workdir()

    # Ensure a local config exists next to the exe / project
    cfg = root / "config.yaml"
    example = root / "config.example.yaml"
    if not cfg.exists() and example.exists():
        cfg.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    if len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS:
        from main import app

        app()
        return

    from gui.desktop import run_desktop

    run_desktop(config_path=cfg)


if __name__ == "__main__":
    main()
