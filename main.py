"""
Moodle Quiz Solver — CLI entry point.

Commands:
  solve          Pass a quiz URL or quiz ID, solve it automatically.
  list-quizzes   Show all available quizzes across enrolled courses.
  setup          Interactive wizard to configure API keys and Moodle URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import signal
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box

app = typer.Typer(
    name="moodle-solver",
    help="Automatically solve Moodle quizzes using AI agents with majority voting.",
    no_args_is_help=True,
)
console = Console()

CONFIG_PATH = Path("config.yaml")

CREATOR = "Kem (Kemuus) — https://github.com/STLKem"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        console.print(
            f"[red]Config file not found: {path}[/red]\n"
            "Run [bold]python main.py setup[/bold] to create it."
        )
        raise typer.Exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def _run_until_interrupt(coro) -> None:
    """Run a coroutine; Ctrl+C cancels it gracefully instead of mid-loop KeyboardInterrupt."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(coro)
    stop_count = 0

    def _request_stop(*_args) -> None:
        nonlocal stop_count
        stop_count += 1
        if stop_count == 1:
            main_task.cancel()
            return
        raise KeyboardInterrupt

    if sys.platform == "win32":
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGINT, _request_stop)
            signal.signal(signal.SIGTERM, _request_stop)
    else:
        with contextlib.suppress(ValueError, OSError, NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, _request_stop)
            loop.add_signal_handler(signal.SIGTERM, _request_stop)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def solve(
    url: Optional[str] = typer.Option(
        None,
        "--url", "-u",
        help="Direct URL to the quiz view page, e.g. https://moodle.uni.edu/mod/quiz/view.php?id=42",
    ),
    quiz_id: Optional[str] = typer.Option(
        None,
        "--quiz-id", "-q",
        help="Moodle quiz ID (integer). Used if --url is not provided.",
    ),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Solve a Moodle quiz automatically."""
    cfg = load_config(config_path)

    # Determine target
    target: Optional[str] = url or quiz_id

    if not target:
        # Interactive selection
        target = _interactive_quiz_select(cfg)
        if not target:
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    # Validate config keys
    _warn_missing_keys(cfg)

    from solver import QuizSolver
    solver = QuizSolver(cfg)
    asyncio.run(solver.run(target))


@app.command(name="list-quizzes")
def list_quizzes(
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """List all available quizzes from enrolled Moodle courses."""
    cfg = load_config(config_path)
    asyncio.run(_async_list_quizzes(cfg))


@app.command(name="benchmark-models")
def benchmark_models(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Update config.yaml to keep only the top models (also sets calls_per_agent=1).",
    ),
    keep: int = typer.Option(
        2,
        "--keep",
        min=1,
        max=6,
        help="How many top models to keep when --apply is used.",
    ),
    timeout_s: float = typer.Option(
        25.0,
        "--timeout",
        min=5.0,
        max=120.0,
        help="Timeout per single model call (seconds).",
    ),
    n: int = typer.Option(
        20,
        "--n",
        min=5,
        max=40,
        help="How many benchmark questions to use (default 20 for speed).",
    ),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Benchmark enabled AI models and optionally keep only the best ones."""
    cfg = load_config(config_path)
    from benchmarks.benchmark_models import main as bench_main
    asyncio.run(
        bench_main(
            config_path=config_path,
            apply=apply,
            keep=keep,
            timeout_s=timeout_s,
            n=n,
        )
    )


@app.command(name="remote-bots")
def remote_bots(
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Run Telegram and/or Discord remote-control bots."""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    remote_cfg = cfg.get("remote", {}) or {}
    moodle_base_url = (cfg.get("moodle", {}) or {}).get("url", "")

    tg_token = remote_cfg.get("telegram_bot_token", "")
    dc_token = remote_cfg.get("discord_bot_token", "")
    allowed_tg = remote_cfg.get("allowed_telegram_user_ids", []) or []
    allowed_dc = remote_cfg.get("allowed_discord_user_ids", []) or []
    runs_dir = Path(str(remote_cfg.get("runs_dir", "remote_runs")))

    console.print(
        f"[dim]remote-bots config: {config_path} "
        f"(tg_allowed={len(allowed_tg)}, dc_allowed={len(allowed_dc)})[/dim]"
    )

    @contextlib.contextmanager
    def _prevent_sleep_windows():
        if sys.platform != "win32":
            yield
            return
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass

    async def _run():
        # Shared managers: ensures start/stop from two bots controls ONE process.
        from remote.runner import (
            ChatBotManager,
            RunManager,
            WatchManager,
            should_auto_start_chat_bot,
        )
        shared_run_mgr = RunManager(runs_dir, moodle_base_url=moodle_base_url, config_path=str(config_path))
        shared_watch_mgr = WatchManager(runs_dir, config_path=str(config_path))
        chat_bot_cfg = cfg.get("chat_bot", {}) or {}
        chat_bot_debug = bool(chat_bot_cfg.get("debug", False))
        shared_chat_mgr: ChatBotManager | None = None
        if should_auto_start_chat_bot(chat_bot_cfg):
            shared_chat_mgr = ChatBotManager(
                runs_dir,
                config_path=str(config_path),
                debug=chat_bot_debug,
            )
            st = await shared_chat_mgr.start()
            console.print(
                f"[green]chat-bot[/green] started in background "
                f"(pid={st.pid}, log={st.log_path})"
            )
            if chat_bot_debug:
                console.print(
                    "[dim]chat-bot debug ON — subprocess output will appear here "
                    "as [chat-bot:subprocess] lines every 15s[/dim]"
                )
            console.print(
                f"[dim]chat-bot config: model={chat_bot_cfg.get('model')!r}, "
                f"conversation_id={chat_bot_cfg.get('conversation_id')}, "
                f"chat_id={chat_bot_cfg.get('chat_id')}, "
                f"poll_seconds={chat_bot_cfg.get('poll_seconds', 5)}[/dim]"
            )

        bot_tasks: list[asyncio.Task] = []
        if tg_token and not str(tg_token).startswith("YOUR"):
            from remote.telegram_bot import start_telegram_bot
            bot_tasks.append(
                asyncio.create_task(
                    start_telegram_bot(
                        tg_token,
                        allowed_tg,
                        runs_dir,
                        moodle_base_url,
                        run_mgr=shared_run_mgr,
                        watch_mgr=shared_watch_mgr,
                    )
                )
            )
        if dc_token and not str(dc_token).startswith("YOUR"):
            from remote.discord_bot import start_discord_bot
            bot_tasks.append(
                asyncio.create_task(
                    start_discord_bot(
                        dc_token,
                        allowed_dc,
                        runs_dir,
                        moodle_base_url,
                        run_mgr=shared_run_mgr,
                        watch_mgr=shared_watch_mgr,
                    )
                )
            )

        if not bot_tasks:
            console.print(
                "[red]No bot tokens configured.[/red]\n"
                "Set remote.telegram_bot_token and/or remote.discord_bot_token in config.yaml"
            )
            raise typer.Exit(1)

        try:
            results = await asyncio.gather(*bot_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    console.print(f"[yellow]Bot failed:[/yellow] {type(r).__name__}: {r}")

            # If Telegram/Discord exited but chat-bot subprocess is still alive, keep running.
            if shared_chat_mgr is not None and shared_chat_mgr.status().running:
                console.print(
                    "[dim]Remote bots stopped. chat-bot still running in background "
                    "— press Ctrl+C to stop everything.[/dim]"
                )
                try:
                    while shared_chat_mgr.status().running:
                        await asyncio.sleep(10)
                except asyncio.CancelledError:
                    pass
                if not shared_chat_mgr.status().running:
                    exit_code = shared_chat_mgr._last_exit_code
                    log_path = shared_chat_mgr.status().log_path
                    console.print(
                        f"[red]chat-bot subprocess died unexpectedly "
                        f"(exit_code={exit_code}). Check log: {log_path}[/red]"
                    )
        except asyncio.CancelledError:
            pass
        finally:
            for t in bot_tasks:
                t.cancel()
            await asyncio.gather(*bot_tasks, return_exceptions=True)
            if shared_chat_mgr is not None:
                await shared_chat_mgr.stop()
                console.print("[dim]chat-bot stopped[/dim]")

    try:
        with _prevent_sleep_windows():
            _run_until_interrupt(_run())
    except KeyboardInterrupt:
        console.print("[dim]remote-bots stopped[/dim]")


@app.command(name="watch-attempts")
def watch_attempts(
    once: bool = typer.Option(
        True,
        "--once/--watch",
        help="--once: single scan and exit; --watch: poll repeatedly.",
    ),
    auto_exclude: bool = typer.Option(
        False,
        "--auto-exclude",
        help="Temporarily hide found quiz_ids for this run only (resets on restart).",
    ),
    list_courses: bool = typer.Option(
        True,
        "--list-courses/--no-list-courses",
        help="Print all enrolled courses the scanner walks (REST: core_enrol_get_users_courses).",
    ),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """List unfinished quiz attempts for the current user (monitoring only)."""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    from monitor_attempts import main as scan_main, watch as watch_loop
    if once:
        code = asyncio.run(scan_main(cfg, list_courses=list_courses))
        raise typer.Exit(code=code)
    asyncio.run(
        watch_loop(
            cfg,
            auto_exclude=auto_exclude,
            config_path=config_path,
            list_courses=list_courses,
        )
    )


@app.command(name="list-chats")
def list_chats(
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """List all personal conversations (not course chat activities)."""
    cfg = load_config(config_path.resolve())
    asyncio.run(_async_list_chats(cfg))


@app.command(name="chat-bot")
def chat_bot(
    conversation_id: Optional[int] = typer.Option(
        None,
        "--conv-id",
        help="core_message conversation ID (personal messages).",
    ),
    chat_id: Optional[int] = typer.Option(
        None,
        "--chat-id",
        help="mod_chat activity ID (Chat activity).",
    ),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Monitor a Moodle chat and reply via AI."""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    moodle_cfg = cfg.get("moodle", {}) or {}
    agent_cfg = cfg.get("agents", {}) or {}
    chat_bot_cfg = cfg.get("chat_bot", {}) or {}

    from moodle_chat_bot import run_chat_bot
    from moodle.chat_client import MoodleChatError
    try:
        asyncio.run(run_chat_bot(
            moodle_cfg=moodle_cfg,
            agent_cfg=agent_cfg,
            chat_bot_cfg=chat_bot_cfg,
            conversation_id_override=conversation_id,
            chat_id_override=chat_id,
        ))
    except MoodleChatError as exc:
        console.print(f"[red][chat-bot][/red] {exc}")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        console.print("[yellow][chat-bot][/yellow] interrupted by user")


@app.command(name="mark-attempt")
def mark_attempt(
    attempt_id: str = typer.Option(
        ...,
        "--attempt-id",
        help="Attempt ID to mark as in_progress (suppresses it from watch output).",
    ),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Mark an attempt as in-progress for monitoring suppression."""
    cfg = load_config(config_path.resolve())
    from monitor_attempts import mark_in_progress
    mark_in_progress(cfg, attempt_id)
    console.print(f"[green]Marked attempt {attempt_id} as in_progress.[/green]")


@app.command(name="gui")
def gui(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (local only recommended)."),
    port: int = typer.Option(8787, "--port", help="Bind port."),
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
    desktop: bool = typer.Option(
        False,
        "--desktop",
        help="Open as a native desktop window (requires pywebview).",
    ),
) -> None:
    """Run the local control panel (browser or desktop window)."""
    config_path = config_path.resolve()
    if desktop:
        from gui.desktop import run_desktop

        console.print("[bold cyan]Opening desktop control panel…[/bold cyan]")
        run_desktop(config_path=config_path, host=host, port=port)
        return

    try:
        import uvicorn  # type: ignore
    except Exception:
        console.print("[red]Missing dependency: uvicorn[/red]")
        console.print("Install: pip install -r requirements.txt")
        raise typer.Exit(1)

    from gui.app import create_app

    app_ = create_app(config_path=str(config_path))
    console.print(f"[bold cyan]GUI[/bold cyan] running at http://{host}:{port}")
    console.print("[dim]Tip: python main.py gui --desktop  → native window[/dim]")
    uvicorn.run(app_, host=host, port=port, log_level="info")


@app.command(name="desktop")
def desktop_cmd(
    config_path: Path = typer.Option(
        CONFIG_PATH,
        "--config", "-c",
        help="Path to config.yaml",
    ),
) -> None:
    """Open the native desktop control panel window."""
    from gui.desktop import run_desktop

    run_desktop(config_path=config_path.resolve())


@app.command()
def setup() -> None:
    """Interactive wizard: configure Moodle credentials and API keys."""
    console.print(
        Panel(
            "[bold cyan]Moodle Quiz Solver — Setup Wizard[/bold cyan]\n"
            "This will create/update your [yellow]config.yaml[/yellow].",
            border_style="cyan",
        )
    )

    # Load existing config if available
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = load_config(CONFIG_PATH)
        console.print("[dim]Found existing config.yaml — press Enter to keep current values.[/dim]\n")

    moodle = cfg.setdefault("moodle", {})
    agents = cfg.setdefault("agents", {})
    solver_cfg = cfg.setdefault("solver", {})

    # Moodle settings
    console.rule("[bold]Moodle Settings[/bold]")
    moodle["url"] = Prompt.ask(
        "Moodle URL (e.g. https://moodle.university.edu)",
        default=moodle.get("url", ""),
    ).rstrip("/")
    moodle["username"] = Prompt.ask("Username", default=moodle.get("username", ""))
    moodle["password"] = Prompt.ask(
        "Password", password=True, default=moodle.get("password", "")
    )
    moodle["preferred_mode"] = Prompt.ask(
        "Connection mode (auto / api / scraper)",
        default=moodle.get("preferred_mode", "auto"),
    )

    # API keys
    console.rule("[bold]AI Agent API Keys[/bold]")
    console.print(
        "  [cyan]Google Gemini[/cyan]:  [link=https://aistudio.google.com]aistudio.google.com[/link]  (free, 1500 req/day)\n"
        "  [cyan]Groq[/cyan]:           [link=https://console.groq.com]console.groq.com[/link]  (free, 14400 req/day)\n"
        "  [cyan]Cerebras[/cyan]:       [link=https://cloud.cerebras.ai]cloud.cerebras.ai[/link]  (free, 30 req/min)\n"
    )
    agents["google_api_key"] = Prompt.ask(
        "Google API Key", default=agents.get("google_api_key", "")
    )
    agents["groq_api_key"] = Prompt.ask(
        "Groq API Key", default=agents.get("groq_api_key", "")
    )
    agents["cerebras_api_key"] = Prompt.ask(
        "Cerebras API Key", default=agents.get("cerebras_api_key", "")
    )

    # Solver preferences
    console.rule("[bold]Solver Preferences[/bold]")
    agents["calls_per_agent"] = int(
        Prompt.ask(
            "Calls per agent per question (1-3, default=2)",
            default=str(agents.get("calls_per_agent", 2)),
        )
    )
    solver_cfg["show_reasoning"] = Confirm.ask(
        "Show agent reasoning in output?",
        default=solver_cfg.get("show_reasoning", True),
    )
    solver_cfg["save_log"] = Confirm.ask(
        "Save quiz log to quiz_log.json?",
        default=solver_cfg.get("save_log", True),
    )

    save_config(cfg)
    console.print(
        "\n[bold green]config.yaml saved.[/bold green] "
        "Run [bold]python main.py solve[/bold] to start."
    )


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _async_list_quizzes(cfg: dict) -> None:
    from moodle.client import ConnectionMode, MoodleClient

    moodle_cfg = cfg.get("moodle", {})
    client = MoodleClient(
        base_url=moodle_cfg["url"],
        username=moodle_cfg["username"],
        password=moodle_cfg["password"],
        preferred_mode=ConnectionMode(moodle_cfg.get("preferred_mode", "auto")),
    )

    console.print("[dim]Connecting to Moodle...[/dim]")
    async with client:
        console.print(f"[dim]Mode: {client.mode.value}[/dim]")
        courses = await client.get_enrolled_courses()

        if not courses:
            console.print("[yellow]No enrolled courses found.[/yellow]")
            return

        for course in courses:
            quizzes = await client.get_quizzes_by_course(course.course_id)
            if not quizzes:
                continue

            table = Table(
                title=f"[bold]{course.full_name}[/bold] (ID: {course.course_id})",
                box=box.SIMPLE,
                show_header=True,
            )
            table.add_column("Quiz ID", style="cyan", no_wrap=True)
            table.add_column("Name", style="white")
            table.add_column("Command", style="dim")

            for q in quizzes:
                table.add_row(
                    q.quiz_id,
                    q.name,
                    f"python main.py solve --quiz-id {q.quiz_id}",
                )
            console.print(table)


def _interactive_quiz_select(cfg: dict) -> Optional[str]:
    """Let the user pick a quiz from a list. Returns quiz_id or URL."""
    console.print(
        "[cyan]No quiz specified.[/cyan] Enter a quiz URL or ID, "
        "or press Enter to list available quizzes:"
    )
    value = Prompt.ask("Quiz URL or ID (empty = show list)", default="")
    if value.strip():
        return value.strip()

    # Show list and let user pick
    quizzes = asyncio.run(_async_get_all_quizzes(cfg))
    if not quizzes:
        return None

    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("ID", style="dim")
    table.add_column("Name", style="white")

    for i, q in enumerate(quizzes, 1):
        table.add_row(str(i), q.quiz_id, q.name)

    console.print(table)
    choice = Prompt.ask(f"Select quiz number (1-{len(quizzes)})")
    try:
        idx = int(choice) - 1
        return quizzes[idx].quiz_id
    except (ValueError, IndexError):
        return None


async def _async_get_all_quizzes(cfg: dict):
    from moodle.client import ConnectionMode, MoodleClient

    moodle_cfg = cfg.get("moodle", {})
    client = MoodleClient(
        base_url=moodle_cfg["url"],
        username=moodle_cfg["username"],
        password=moodle_cfg["password"],
        preferred_mode=ConnectionMode(moodle_cfg.get("preferred_mode", "auto")),
    )
    async with client:
        return await client.get_all_quizzes()


async def _async_list_chats(cfg: dict) -> None:
    """List personal conversations and chat activities."""
    from moodle.chat_client import MoodleChatClient
    from moodle.api import MoodleAPI

    moodle_cfg = cfg.get("moodle", {})
    console.print("[dim]Connecting to Moodle...[/dim]")
    async with MoodleAPI(
        moodle_cfg["url"],
        moodle_cfg["username"],
        moodle_cfg["password"],
    ) as api:
        chat = MoodleChatClient(api)

        # --- Personal conversations (core_message) ---
        self_conv = await chat.find_self_conversation()
        all_convs = await chat.get_conversations()

        if self_conv or all_convs:
            table = Table(box=box.SIMPLE, show_header=True, title="Personal Conversations (core_message)")
            table.add_column("Type", style="cyan", no_wrap=True)
            table.add_column("Name", style="white")
            table.add_column("ID", style="dim")
            table.add_column("Command", style="green")

            if self_conv:
                table.add_row(
                    "self",
                    self_conv.name or "Self-conversation",
                    str(self_conv.id),
                    f"python main.py chat-bot --conv-id {self_conv.id}",
                )
            type_names = {1: "private", 2: "group", 3: "self"}
            for c in all_convs:
                if self_conv and c.id == self_conv.id:
                    continue
                table.add_row(
                    type_names.get(c.type, str(c.type)),
                    c.name or f"Conversation {c.id}",
                    str(c.id),
                    f"python main.py chat-bot --conv-id {c.id}",
                )
            console.print(table)

        # --- Chat activities (mod_chat) from enrolled courses ---
        chat_activities: list[tuple[str, str, str]] = []
        try:
            info = await api._call("core_webservice_get_site_info")
            uid = info["userid"]
            courses = await api._call("core_enrol_get_users_courses", userid=uid)
            for c in list(courses)[:10]:
                try:
                    raw = await api._call(
                        "mod_chat_get_chats_by_courses",
                        **{"courseids[0]": str(c["id"])},
                    )
                    for ch in raw.get("chats", []):
                        chat_activities.append((
                            c.get("shortname", ""),
                            ch.get("name", ""),
                            str(ch["id"]),
                        ))
                except Exception:
                    pass
        except Exception:
            pass

        if chat_activities:
            table2 = Table(box=box.SIMPLE, show_header=True, title="Chat Activities (mod_chat)")
            table2.add_column("Course", style="cyan", no_wrap=True)
            table2.add_column("Name", style="white")
            table2.add_column("chat_id", style="dim")
            table2.add_column("Command", style="green")
            for course_name, ch_name, ch_id in sorted(chat_activities):
                table2.add_row(
                    course_name,
                    ch_name,
                    ch_id,
                    f"python main.py chat-bot --chat-id {ch_id}",
                )
            console.print(table2)

        if not self_conv and not all_convs and not chat_activities:
            console.print("[yellow]No conversations or chat activities found.[/yellow]")
            return

        console.print(
            "\n[bold]To start the chat bot:[/bold]\n"
            "  [green]python main.py chat-bot --conv-id <ID>[/green] (personal messages)\n"
            "  [green]python main.py chat-bot --chat-id <ID>[/green] (Chat activity)\n"
            "Or add to config.yaml:\n"
            "  [yellow]chat_bot:\n"
            "    chat_id: <ID>\n"
            "    poll_seconds: 5\n"
            "    model: groq_qwen3_32b[/yellow]"
        )


def _warn_missing_keys(cfg: dict) -> None:
    from agents.keys import is_valid_key

    agents = cfg.get("agents", {})
    checks = [
        ("google_api_key", "Google"),
        ("groq_api_key", "Groq"),
        ("cerebras_api_key", "Cerebras"),
        ("deepseek_api_key", "DeepSeek"),
        ("openrouter_api_key", "OpenRouter"),
        ("github_api_key", "GitHub"),
    ]
    missing = [
        label for field, label in checks if not is_valid_key(agents.get(field, ""))
    ]
    if missing:
        console.print(
            f"[yellow]Warning:[/yellow] Missing or placeholder API keys: {', '.join(missing)}\n"
            "Providers without keys will be skipped. "
            "Run [bold]python main.py setup[/bold] or edit config.yaml."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    console.print(f"[dim]Created by {CREATOR}[/dim]")
    app()
