from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table
from rich import box

from moodle.client import ConnectionMode, MoodleClient
from moodle.api import MoodleAPIError
from moodle.models import CourseInfo


console = Console()

_STATUS_IN_PROGRESS = "in_progress"
_STATUS_STARTED = "started"


@dataclass
class FoundAttempt:
    course_id: str
    course_name: str
    quiz_id: str
    view_id: str
    view_url: str
    quiz_name: str
    attempt_id: str
    state: str
    timestart: Any
    timefinish: Any


async def scan_unfinished(cfg: dict) -> tuple[list[FoundAttempt], list[CourseInfo]]:
    moodle_cfg = cfg.get("moodle", {}) or {}
    base_url = str(moodle_cfg.get("url", "")).rstrip("/")
    mon_cfg = cfg.get("monitor", {}) or {}
    exclude_quiz_ids = set(str(x) for x in (mon_cfg.get("exclude_quiz_ids", []) or []))

    client = MoodleClient(
        base_url=moodle_cfg["url"],
        username=moodle_cfg["username"],
        password=moodle_cfg["password"],
        # Monitoring requires REST API; never fall back to scraper here.
        preferred_mode=ConnectionMode.API,
    )

    found: list[FoundAttempt] = []
    courses: list[CourseInfo] = []
    try:
        async with client:
            if client.mode != ConnectionMode.API:
                console.print(
                    "[yellow]Monitor mode works only with REST API.[/yellow] "
                    "Scraper mode can't efficiently list unfinished attempts."
                )
                return [], []

            courses = await client.get_enrolled_courses()
            for course in courses:
                quizzes = await client.get_quizzes_by_course(course.course_id)
                quizid_to_view: dict[str, str] = {}
                for q in quizzes:
                    if getattr(q, "cmid", None):
                        quizid_to_view[str(q.quiz_id)] = str(q.cmid)
                for quiz in quizzes:
                    if str(quiz.quiz_id) in exclude_quiz_ids:
                        continue
                    attempts = await client.list_unfinished_attempts(quiz.quiz_id)
                    for a in attempts:
                        view_id = quizid_to_view.get(str(quiz.quiz_id), "")
                        view_url = f"{base_url}/mod/quiz/view.php?id={view_id}" if view_id else ""
                        found.append(
                            FoundAttempt(
                                course_id=str(course.course_id),
                                course_name=str(course.full_name),
                                quiz_id=str(quiz.quiz_id),
                                view_id=view_id,
                                view_url=view_url,
                                quiz_name=str(quiz.name),
                                attempt_id=str(a.get("id", "")),
                                state=str(a.get("state", "")),
                                timestart=a.get("timestart"),
                                timefinish=a.get("timefinish"),
                            )
                        )
            return found, courses
    except MoodleAPIError as exc:
        console.print(f"[red]REST API login failed:[/red] {exc}")
        console.print(
            "[dim]Fix: ensure Moodle Web Services are reachable from this server, "
            "or try again later. watch-attempts cannot run in scraper mode.[/dim]"
        )
        return [], []


def _state_path(cfg: dict) -> Path:
    mon_cfg = cfg.get("monitor", {}) or {}
    return Path(str(mon_cfg.get("state_file", "monitor_state.json")))


def load_state(cfg: dict) -> dict[str, str]:
    path = _state_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}
    return {}


def save_state(cfg: dict, state: dict[str, str]) -> None:
    path = _state_path(cfg)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_in_progress(cfg: dict, attempt_id: str) -> None:
    state = load_state(cfg)
    state[str(attempt_id)] = _STATUS_IN_PROGRESS
    save_state(cfg, state)


def mark_started(cfg: dict, attempt_id: str) -> None:
    """
    Persist that watch-attempts already started solving this attempt.
    Prevents re-starting the same attempt after a watch restart (until attempt disappears or state cleared on error).
    """
    state = load_state(cfg)
    if state.get(str(attempt_id)) != _STATUS_IN_PROGRESS:
        state[str(attempt_id)] = _STATUS_STARTED
        save_state(cfg, state)


def render_courses_scanned(courses: list[CourseInfo]) -> None:
    """Courses returned by core_enrol_get_users_courses (same scope as watch-attempts scan)."""
    if not courses:
        console.print("[dim]No enrolled courses returned by API.[/dim]")
        return
    table = Table(
        title="Enrolled courses (core_enrol_get_users_courses — full scan scope)",
        box=box.SIMPLE,
        show_header=True,
    )
    table.add_column("Course ID", style="dim", no_wrap=True)
    table.add_column("Short name", style="cyan", no_wrap=True)
    table.add_column("Full name", style="white")
    for c in sorted(courses, key=lambda x: (x.full_name or "").lower()):
        table.add_row(str(c.course_id), str(c.short_name or "-"), str(c.full_name or "-"))
    console.print(table)
    console.print(f"[dim]Total courses scanned: {len(courses)}[/dim]")


def render(found: list[FoundAttempt], *, example_source: list[FoundAttempt] | None = None) -> None:
    if not found:
        console.print("[green]No unfinished attempts found.[/green]")
    else:
        table = Table(title="Unfinished quiz attempts (current user)", box=box.SIMPLE, show_header=True)
        table.add_column("Course", style="white")
        table.add_column("Quiz", style="cyan")
        table.add_column("View ID", style="dim", no_wrap=True)
        table.add_column("Quiz ID", style="dim", no_wrap=True)
        table.add_column("Attempt ID", style="dim", no_wrap=True)
        table.add_column("State", style="yellow", no_wrap=True)
        for f in found:
            table.add_row(
                f"{f.course_name} ({f.course_id})",
                f.quiz_name,
                f.view_id or "-",
                f.quiz_id,
                f.attempt_id,
                f.state,
            )
        console.print(table)

    show_for = example_source if example_source is not None else found

    view_ids = sorted({f.view_id for f in show_for if f.view_id})
    if view_ids:
        console.print("\nView IDs (the last digits in view.php?id=...):")
        console.print(str(view_ids))

    quiz_ids = sorted({f.quiz_id for f in show_for})
    console.print("\nAdd these to config.yaml → monitor.exclude_quiz_ids if needed:")
    console.print(str(quiz_ids))

    # Dry-run examples (NO execution): show how a solve would be invoked.
    # This is helpful for manual QA workflows.
    if show_for:
        example = show_for[0]
        console.print("\nExample commands (dry-run, not executed):")
        if example.view_url:
            console.print(f'  python main.py solve --url "{example.view_url}"')
        console.print(f'  python main.py solve --quiz-id {example.quiz_id}')
        # Example ONLY (commented out): how "print a command" differs from
        # actually executing code.
        #
        # IMPORTANT: This is a template for understanding only.
        # It is intentionally commented out so it cannot run by accident.
        # ------------------------------------------------------------------
        #
        # Option A) Call the solver directly (in-process):
        #
        # import asyncio
        # from solver import QuizSolver
        # cfg = yaml.safe_load(Path(r"C:\Users\oneto\Desktop\Program For Moodle\config.yaml").read_text(encoding="utf-8")) # load config dict
        # s = QuizSolver(cfg)
        # asyncio.run(s.run(str(example.quiz_id)))   # or s.run(example.view_url)
        #
        # Option B) Spawn a separate process (like a terminal command):
        #
        # import subprocess
        # subprocess.run(
        #     ["python", "main.py", "solve", "--quiz-id", str(example.quiz_id)],
        #     check=False,
        # )


async def watch(
    cfg: dict,
    *,
    auto_exclude: bool = False,
    config_path: Path | None = None,
    list_courses: bool = True,
) -> None:
    mon_cfg = cfg.get("monitor", {}) or {}
    poll = int(mon_cfg.get("poll_seconds", 60) or 60)
    jitter = int(mon_cfg.get("poll_jitter_seconds", 0) or 0)
    state = load_state(cfg)
    temp_excluded_quiz_ids: set[str] = set()
    # In-memory + disk "started": no second solve for same attempt_id in one run or after restart.
    started_attempt_ids: set[str] = {
        aid for aid, st in state.items() if st == _STATUS_STARTED
    }
    solve_lock = asyncio.Lock()
    running_solve: asyncio.Task[None] | None = None
    courses_printed = not list_courses

    def _solve_already_recorded(attempt_id: str) -> bool:
        if attempt_id in started_attempt_ids:
            return True
        return state.get(attempt_id) == _STATUS_STARTED

    async def _run_solve_for_attempt(a: FoundAttempt) -> None:
        async with solve_lock:
            console.print(
                f"[bold yellow]Starting solve[/bold yellow] "
                f"(quiz_id={a.quiz_id}, attempt_id={a.attempt_id})"
            )
            try:
                args: list[str] = ["python", "main.py", "solve"]
                if a.view_url:
                    args += ["--url", str(a.view_url)]
                else:
                    args += ["--quiz-id", str(a.quiz_id)]
                if config_path is not None:
                    args += ["--config", str(config_path)]

                console.print(f"[dim]Launching:[/dim] {' '.join(args)}")
                process = await asyncio.create_subprocess_exec(*args, stdout=None, stderr=None)
                await process.wait()
                console.print(
                    f"[green]Solve finished[/green] "
                    f"(quiz_id={a.quiz_id}, attempt_id={a.attempt_id}, exit={process.returncode})"
                )
                if process.returncode != 0:
                    # Allow a later poll to retry after a failed run.
                    state.pop(a.attempt_id, None)
                    started_attempt_ids.discard(a.attempt_id)
                    save_state(cfg, state)
                    console.print(
                        "[yellow]Non-zero exit:[/yellow] cleared started state for this attempt "
                        f"(attempt_id={a.attempt_id}) so the next scan can retry."
                    )
            except Exception as e:
                console.print(
                    f"[red]Solve failed[/red] "
                    f"(quiz_id={a.quiz_id}, attempt_id={a.attempt_id}): {e}"
                )
                state.pop(a.attempt_id, None)
                started_attempt_ids.discard(a.attempt_id)
                save_state(cfg, state)
                console.print(
                    "[yellow]Exception during solve:[/yellow] cleared started state for this attempt "
                    f"(attempt_id={a.attempt_id}) so the next scan can retry."
                )
    while True:
        found, courses = await scan_unfinished(cfg)
        if list_courses and not courses_printed and courses:
            render_courses_scanned(courses)
            courses_printed = True
        if auto_exclude and found:
            newly = sorted({str(f.quiz_id) for f in found} - temp_excluded_quiz_ids)
            if newly:
                temp_excluded_quiz_ids.update(newly)
                console.print(f"[yellow]Auto-excluded for this run:[/yellow] {newly}")

        active_attempt_ids = {f.attempt_id for f in found}
        # Cleanup: remove state entries that no longer exist
        removed = [aid for aid in list(state.keys()) if aid not in active_attempt_ids]
        for aid in removed:
            state.pop(aid, None)
        if removed:
            save_state(cfg, state)

        # Keep showing attempts unless user marked them as in_progress
        visible = [
            f
            for f in found
            if state.get(f.attempt_id) != _STATUS_IN_PROGRESS
            and str(f.quiz_id) not in temp_excluded_quiz_ids
        ]

        if visible:
            candidate: FoundAttempt | None = None
            for a in visible:
                if not _solve_already_recorded(a.attempt_id):
                    candidate = a
                    break

            if candidate is not None:
                if running_solve is None or running_solve.done():
                    started_attempt_ids.add(candidate.attempt_id)
                    mark_started(cfg, candidate.attempt_id)
                    state[candidate.attempt_id] = _STATUS_STARTED
                    running_solve = asyncio.create_task(_run_solve_for_attempt(candidate))
                else:
                    console.print("[dim]Solve already running; waiting for next cycle.[/dim]")
            else:
                console.print(
                    "[dim]No new solve: visible unfinished attempt(s) are already marked "
                    "started (or solve still running).[/dim]"
                )
        console.rule()
        sleep_s = poll
        if jitter > 0:
            sleep_s = max(5, poll + random.randint(-jitter, jitter))
        console.print(f"[dim]scan complete; visible={len(visible)}/{len(found)}; next in {sleep_s}s[/dim]")
        render(visible, example_source=found)
        await asyncio.sleep(sleep_s)


async def main(cfg: dict, *, list_courses: bool = True) -> int:
    found, courses = await scan_unfinished(cfg)
    if list_courses:
        render_courses_scanned(courses)
    render(found)
    return 1 if found else 0

