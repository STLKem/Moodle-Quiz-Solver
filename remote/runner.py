from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional


_DIGITS_RE = re.compile(r"^\s*(?:solve\s+)?(\d+)\s*$", re.IGNORECASE)
_VIEW_URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
_VIEW_ID_RE = re.compile(r"mod/quiz/view\.php\?id=(\d+)", re.IGNORECASE)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def cli_command(*args: str) -> list[str]:
    """
    Build a subprocess argv for Moodle Solver CLI commands.

    Works both in normal Python runs and when packaged as a frozen exe.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-u", str(_PROJECT_ROOT / "main.py"), *args]


def project_workdir(config_path: str | None = None) -> Path:
    if config_path:
        return Path(config_path).resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _PROJECT_ROOT


def parse_view_id_or_url(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None

    m = _DIGITS_RE.match(raw)
    if m:
        return m.group(1)

    url_m = _VIEW_URL_RE.search(raw)
    if url_m:
        url = url_m.group(1)
        id_m = _VIEW_ID_RE.search(url)
        if id_m:
            return id_m.group(1)
    return None


@dataclass
class RunResult:
    view_id: str
    started_at: str
    finished_at: str
    exit_code: int
    log_path: str


class RunManager:
    def __init__(
        self,
        runs_dir: Path,
        moodle_base_url: str,
        *,
        config_path: str | None = None,
    ) -> None:
        self.runs_dir = runs_dir
        self.moodle_base_url = moodle_base_url.rstrip("/")
        self.config_path = config_path
        self._lock = asyncio.Lock()

    def is_busy(self) -> bool:
        return self._lock.locked()

    async def run_solve(self, view_id: str) -> RunResult:
        async with self._lock:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = self.runs_dir / f"{ts}_view_{view_id}.log"
            started = datetime.now().isoformat(timespec="seconds")

            url = f"{self.moodle_base_url}/mod/quiz/view.php?id={view_id}"
            args = cli_command("solve", "--url", url)
            if self.config_path:
                args += ["--config", self.config_path]

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_workdir(self.config_path)),
                env=env,
            )

            assert proc.stdout is not None
            with open(log_path, "w", encoding="utf-8") as f:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    f.write(chunk.decode("utf-8", errors="replace"))
                    f.flush()

            code = await proc.wait()
            finished = datetime.now().isoformat(timespec="seconds")
            return RunResult(
                view_id=str(view_id),
                started_at=started,
                finished_at=finished,
                exit_code=int(code or 0),
                log_path=str(log_path),
            )


@dataclass
class WatchStatus:
    running: bool
    started_at: str | None = None
    pid: int | None = None
    started_by: str | None = None
    log_path: str | None = None


class WatchManager:
    """
    Manages a single long-running `watch-attempts --watch` subprocess.
    Ensures only ONE instance is running even if both bots receive 'start'.
    """

    def __init__(self, runs_dir: Path, *, config_path: str | None = None) -> None:
        self.runs_dir = runs_dir
        self.config_path = config_path
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._started_at: str | None = None
        self._started_by: str | None = None
        self._log_path: Path | None = None

    def status(self) -> WatchStatus:
        running = self._proc is not None and self._proc.returncode is None
        return WatchStatus(
            running=running,
            started_at=self._started_at,
            pid=getattr(self._proc, "pid", None) if running else None,
            started_by=self._started_by if running else None,
            log_path=str(self._log_path) if self._log_path else None,
        )

    async def start_watch(
        self,
        *,
        started_by: str,
        on_update: Callable[[str], Awaitable[None]] | None = None,
    ) -> WatchStatus:
        async with self._lock:
            st = self.status()
            if st.running:
                return st

            self.runs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = self.runs_dir / f"{ts}_watch_attempts.log"
            self._log_path = log_path
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._started_by = started_by

            args = cli_command(
                "watch-attempts",
                "--watch",
                "--no-list-courses",
            )
            if self.config_path:
                args += ["--config", self.config_path]

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_workdir(self.config_path)),
                env=env,
            )
            self._proc = proc

            async def _read_stdout() -> None:
                assert proc.stdout is not None
                with open(log_path, "w", encoding="utf-8") as f:
                    while True:
                        chunk = await proc.stdout.readline()
                        if not chunk:
                            break
                        line = chunk.decode("utf-8", errors="replace").rstrip()
                        f.write(line + "\n")
                        f.flush()
                        if not on_update:
                            continue

                        # Send only simplified progress lines.
                        msg: str | None = None
                        if "→ ANSWER:" in line:
                            msg = "ANSWER: " + line.split("→ ANSWER:", 1)[1].strip()
                        elif line.lower().startswith("question ") and "/" in line:
                            msg = line.strip()

                        if msg:
                            try:
                                await on_update(msg)
                            except Exception:
                                pass

                code = await proc.wait()
                if on_update:
                    with contextlib.suppress(Exception):
                        await on_update(f"watch-attempts exited (code={code})")

                # Reset state once finished
                self._proc = None
                self._started_at = None
                self._started_by = None

            self._reader_task = asyncio.create_task(_read_stdout())
            return self.status()

    async def stop_watch(self) -> WatchStatus:
        async with self._lock:
            st = self.status()
            if not st.running:
                return st
            assert self._proc is not None
            proc = self._proc
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

            if self._reader_task:
                self._reader_task.cancel()
                self._reader_task = None
            self._proc = None
            self._started_at = None
            self._started_by = None
            return self.status()


def should_auto_start_chat_bot(chat_bot_cfg: dict) -> bool:
    """Whether chat-bot should run alongside remote-bots."""
    if not chat_bot_cfg:
        return False
    if chat_bot_cfg.get("enabled") is False:
        return False
    if chat_bot_cfg.get("auto_start_with_remote_bots") is False:
        return False
    return bool(str(chat_bot_cfg.get("model", "")).strip())


@dataclass
class ChatBotStatus:
    running: bool
    started_at: str | None = None
    pid: int | None = None
    log_path: str | None = None


class ChatBotManager:
    """
    Runs `python main.py chat-bot` in a separate subprocess so Moodle chat
    polling does not block watch-attempts or Telegram/Discord handlers.
    """

    def __init__(
        self,
        runs_dir: Path,
        *,
        config_path: str | None = None,
        debug: bool = False,
    ) -> None:
        self.runs_dir = runs_dir
        self.config_path = config_path
        self.debug = debug
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._started_at: str | None = None
        self._log_path: Path | None = None
        self._last_exit_code: int | None = None
        self._last_cmd: list[str] | None = None
        self._stopping = False

    def _workdir(self) -> Path:
        return project_workdir(self.config_path)

    def status(self) -> ChatBotStatus:
        running = self._proc is not None and self._proc.returncode is None
        return ChatBotStatus(
            running=running,
            started_at=self._started_at,
            pid=getattr(self._proc, "pid", None) if running else None,
            log_path=str(self._log_path) if self._log_path else None,
        )

    def _debug_print(self, msg: str) -> None:
        if self.debug:
            print(f"[chat-bot:manager] {msg}", flush=True)

    async def start(self) -> ChatBotStatus:
        async with self._lock:
            st = self.status()
            if st.running:
                return st

            self.runs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = self.runs_dir / f"{ts}_chat_bot.log"
            self._log_path = log_path
            self._started_at = datetime.now().isoformat(timespec="seconds")
            self._last_exit_code = None

            workdir = self._workdir()
            args = cli_command("chat-bot")
            if self.config_path:
                args += ["--config", self.config_path]
            self._last_cmd = args

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            if self.debug:
                env["CHAT_BOT_DEBUG"] = "1"

            self._debug_print(f"cwd={workdir}")
            self._debug_print(f"cmd={' '.join(args)}")

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(workdir),
                env=env,
            )
            self._proc = proc
            self._debug_print(f"subprocess started pid={proc.pid}")

            async def _read_stdout() -> None:
                assert proc.stdout is not None
                with open(log_path, "w", encoding="utf-8") as f:
                    while True:
                        chunk = await proc.stdout.readline()
                        if not chunk:
                            break
                        line = chunk.decode("utf-8", errors="replace").rstrip()
                        f.write(line + "\n")
                        f.flush()
                        if self.debug and line:
                            print(f"[chat-bot:subprocess] {line}", flush=True)

                code = int(await proc.wait() or 0)
                self._last_exit_code = code
                self._proc = None
                self._started_at = None
                if code != 0 and not self._stopping:
                    print(
                        f"[chat-bot] subprocess exited with code {code}. "
                        f"See log: {log_path}",
                        flush=True,
                    )
                elif self.debug:
                    self._debug_print(f"subprocess exited cleanly (code=0)")

            self._reader_task = asyncio.create_task(_read_stdout())
            if self.debug:
                self._watch_task = asyncio.create_task(self._watch_loop())
            return self.status()

    async def _watch_loop(self) -> None:
        """Periodic health check when debug mode is on."""
        tick = 0
        while True:
            await asyncio.sleep(15)
            tick += 1
            st = self.status()
            if st.running:
                self._debug_print(
                    f"watch tick={tick} pid={st.pid} running OK log={st.log_path}"
                )
            else:
                print(
                    f"[chat-bot:manager] watch tick={tick} NOT RUNNING "
                    f"(exit_code={self._last_exit_code}, log={st.log_path})",
                    flush=True,
                )
                if self._last_cmd:
                    self._debug_print(f"last cmd={' '.join(self._last_cmd)}")
                return

    async def stop(self) -> ChatBotStatus:
        async with self._lock:
            st = self.status()
            if not st.running:
                return st
            self._stopping = True
            assert self._proc is not None
            proc = self._proc
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

            if self._watch_task:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
                self._watch_task = None
            if self._reader_task:
                self._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reader_task
                self._reader_task = None
            self._proc = None
            self._started_at = None
            return self.status()



@dataclass
class ServiceStatus:
    running: bool
    started_at: str | None = None
    pid: int | None = None
    log_path: str | None = None


class ManagedService:
    """Generic long-running CLI subprocess with log capture."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        config_path: str | None = None,
        command: tuple[str, ...] = (),
        log_suffix: str = "service",
    ) -> None:
        self.runs_dir = runs_dir
        self.config_path = config_path
        self.command = command
        self.log_suffix = log_suffix
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._started_at: str | None = None
        self._log_path: Path | None = None

    def status(self) -> ServiceStatus:
        running = self._proc is not None and self._proc.returncode is None
        return ServiceStatus(
            running=running,
            started_at=self._started_at,
            pid=getattr(self._proc, "pid", None) if running else None,
            log_path=str(self._log_path) if self._log_path else None,
        )

    async def start(self) -> ServiceStatus:
        async with self._lock:
            st = self.status()
            if st.running:
                return st

            self.runs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = self.runs_dir / f"{ts}_{self.log_suffix}.log"
            self._log_path = log_path
            self._started_at = datetime.now().isoformat(timespec="seconds")

            args = cli_command(*self.command)
            if self.config_path:
                args += ["--config", self.config_path]

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_workdir(self.config_path)),
                env=env,
            )
            self._proc = proc

            async def _read_stdout() -> None:
                assert proc.stdout is not None
                with open(log_path, "w", encoding="utf-8") as f:
                    while True:
                        chunk = await proc.stdout.readline()
                        if not chunk:
                            break
                        f.write(chunk.decode("utf-8", errors="replace"))
                        f.flush()
                await proc.wait()
                self._proc = None
                self._started_at = None

            self._reader_task = asyncio.create_task(_read_stdout())
            return self.status()

    async def stop(self) -> ServiceStatus:
        async with self._lock:
            st = self.status()
            if not st.running:
                return st
            assert self._proc is not None
            proc = self._proc
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if self._reader_task:
                self._reader_task.cancel()
                self._reader_task = None
            self._proc = None
            self._started_at = None
            return self.status()


class RemoteBotsManager(ManagedService):
    def __init__(self, runs_dir: Path, *, config_path: str | None = None) -> None:
        super().__init__(
            runs_dir,
            config_path=config_path,
            command=("remote-bots",),
            log_suffix="remote_bots",
        )
