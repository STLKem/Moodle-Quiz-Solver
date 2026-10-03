"""
Moodle Solver — local control panel API.

Serves a desktop-friendly SPA and JSON endpoints for settings, runs, logs,
and attempt history.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents.registry import PROVIDER_KEYS, catalog_entries
from remote.runner import (
    ChatBotManager,
    RemoteBotsManager,
    RunManager,
    WatchManager,
    parse_view_id_or_url,
)


def _resource_root() -> Path:
    """Project / bundle root (handles PyInstaller extractions)."""
    import sys

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def _static_dir() -> Path:
    bundled = _resource_root() / "gui" / "static"
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _mask_secret(value: str) -> str:
    text = (value or "").strip()
    if not text or text.upper().startswith("YOUR"):
        return ""
    if len(text) <= 8:
        return "••••••••"
    return text[:4] + "•" * max(4, len(text) - 8) + text[-4:]


def _public_config(cfg: dict) -> dict:
    """Return config safe for the UI (secrets masked, structure intact)."""
    out = json.loads(json.dumps(cfg))  # deep copy via JSON
    agents = out.setdefault("agents", {})
    for key in PROVIDER_KEYS:
        raw = str(agents.get(key, "") or "")
        agents[key] = raw
        agents[f"{key}__masked"] = _mask_secret(raw)

    remote = out.setdefault("remote", {})
    for key in ("telegram_bot_token", "discord_bot_token"):
        raw = str(remote.get(key, "") or "")
        remote[key] = raw
        remote[f"{key}__masked"] = _mask_secret(raw)

    moodle = out.setdefault("moodle", {})
    pwd = str(moodle.get("password", "") or "")
    moodle["password"] = pwd
    moodle["password__masked"] = _mask_secret(pwd)
    return out


def _agent_catalog(cfg: dict) -> list[dict[str, Any]]:
    return catalog_entries(cfg.get("agents", {}) or {})


def _set_model_list_enabled(entries: list[dict], model: str, enabled: bool) -> bool:
    found = False
    for entry in entries:
        if str(entry.get("model") or "") == model:
            entry["enabled"] = bool(enabled)
            found = True
    return found


def _parse_id_list(raw: Any) -> list[Any]:
    """Accept list or comma/newline separated string of ints/strings."""
    if isinstance(raw, list):
        out: list[Any] = []
        for item in raw:
            text = str(item).strip()
            if not text:
                continue
            out.append(int(text) if text.isdigit() else text)
        return out
    if raw is None:
        return []
    parts = re.split(r"[\s,;]+", str(raw).strip())
    out = []
    for part in parts:
        if not part:
            continue
        out.append(int(part) if part.isdigit() else part)
    return out


def _runs_dir(cfg: dict, cfg_path: Path) -> Path:
    remote = cfg.get("remote", {}) or {}
    raw = Path(str(remote.get("runs_dir", "remote_runs")))
    if not raw.is_absolute():
        raw = cfg_path.parent / raw
    return raw


def _logs_dir(cfg: dict, cfg_path: Path) -> Path:
    solver = cfg.get("solver", {}) or {}
    raw = Path(str(solver.get("logs_dir", "logs")))
    if not raw.is_absolute():
        raw = cfg_path.parent / raw
    return raw


def _list_run_logs(runs_dir: Path) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "kind": _guess_log_kind(path.name),
            }
        )
    return items[:200]


def _guess_log_kind(name: str) -> str:
    lower = name.lower()
    if "watch" in lower:
        return "watch"
    if "chat" in lower:
        return "chat"
    if "remote" in lower or "bot" in lower:
        return "bots"
    if "view_" in lower:
        return "solve"
    return "other"


def _list_attempt_history(logs_dir: Path) -> list[dict[str, Any]]:
    if not logs_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for folder in sorted(
        logs_dir.glob("attempt_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if not folder.is_dir():
            continue
        summary_json = folder / "attempt_summary.json"
        summary_txt = folder / "attempt_summary.txt"
        summary: dict[str, Any] = {}
        if summary_json.exists():
            try:
                summary = json.loads(summary_json.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
        questions = [
            p.name
            for p in sorted(folder.iterdir())
            if p.is_dir() and p.name.startswith("q")
        ]
        items.append(
            {
                "id": folder.name,
                "path": str(folder),
                "mtime": datetime.fromtimestamp(folder.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
                "has_summary": summary_json.exists() or summary_txt.exists(),
                "summary": summary,
                "question_count": len(questions),
                "questions": questions[:50],
            }
        )
    return items[:100]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ConfigUpdate(BaseModel):
    moodle: dict[str, Any] = Field(default_factory=dict)
    agents: dict[str, Any] = Field(default_factory=dict)
    solver: dict[str, Any] = Field(default_factory=dict)
    monitor: dict[str, Any] = Field(default_factory=dict)
    remote: dict[str, Any] = Field(default_factory=dict)
    chat_bot: dict[str, Any] = Field(default_factory=dict)


class SolveRequest(BaseModel):
    view: str


class AgentToggleRequest(BaseModel):
    id: str
    enabled: bool


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(*, config_path: str = "config.yaml") -> FastAPI:
    cfg_path = Path(config_path).resolve()
    app = FastAPI(title="Moodle Solver Control Panel", docs_url=None, redoc_url=None)
    static_dir = _static_dir()

    run_mgr: Optional[RunManager] = None
    watch_mgr: Optional[WatchManager] = None
    bots_mgr: Optional[RemoteBotsManager] = None
    chat_mgr: Optional[ChatBotManager] = None
    active_solve_log: Optional[str] = None

    def _ensure_managers() -> None:
        nonlocal run_mgr, watch_mgr, bots_mgr, chat_mgr
        if run_mgr and watch_mgr and bots_mgr and chat_mgr:
            return
        cfg = _load_config(cfg_path)
        moodle_base = (cfg.get("moodle", {}) or {}).get("url", "")
        runs_dir = _runs_dir(cfg, cfg_path)
        run_mgr = RunManager(runs_dir, moodle_base_url=moodle_base, config_path=str(cfg_path))
        watch_mgr = WatchManager(runs_dir, config_path=str(cfg_path))
        bots_mgr = RemoteBotsManager(runs_dir, config_path=str(cfg_path))
        chat_debug = bool((cfg.get("chat_bot", {}) or {}).get("debug", False))
        chat_mgr = ChatBotManager(runs_dir, config_path=str(cfg_path), debug=chat_debug)

    @app.on_event("startup")
    async def _startup() -> None:
        _ensure_managers()

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(500, "GUI assets missing")
        return FileResponse(index_path)

    # ---- status / config -------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> dict:
        _ensure_managers()
        assert watch_mgr and run_mgr and bots_mgr and chat_mgr
        cfg = _load_config(cfg_path)
        return {
            "config_path": str(cfg_path),
            "watch": watch_mgr.status().__dict__,
            "bots": bots_mgr.status().__dict__,
            "chat": chat_mgr.status().__dict__,
            "solve_busy": run_mgr.is_busy(),
            "active_solve_log": active_solve_log,
            "moodle_url": (cfg.get("moodle", {}) or {}).get("url", ""),
            "agent_count": len(
                [
                    row
                    for row in _agent_catalog(cfg)
                    if row["enabled"]
                ]
            ),
        }

    @app.get("/api/config")
    async def api_get_config() -> dict:
        cfg = _load_config(cfg_path)
        return {
            "config": _public_config(cfg),
            "agents_catalog": _agent_catalog(cfg),
        }

    @app.put("/api/config")
    async def api_put_config(body: ConfigUpdate) -> dict:
        cfg = _load_config(cfg_path)

        def merge_section(name: str, incoming: dict) -> None:
            if not incoming:
                return
            section = cfg.setdefault(name, {})
            for key, value in incoming.items():
                if key.endswith("__masked"):
                    continue
                # Keep existing secret if UI sent blank/masked placeholder
                if isinstance(value, str) and (value == "" or "•" in value):
                    continue
                if key == "enabled" and isinstance(value, dict):
                    section.setdefault("enabled", {})
                    section["enabled"].update({str(k): bool(v) for k, v in value.items()})
                    continue
                if key in ("deepseek_models", "openrouter_models") and isinstance(value, list):
                    cleaned = []
                    for entry in value:
                        if not isinstance(entry, dict):
                            continue
                        model = str(entry.get("model") or "").strip()
                        if not model:
                            continue
                        cleaned.append(
                            {"model": model, "enabled": bool(entry.get("enabled", False))}
                        )
                    section[key] = cleaned
                    continue
                if key in (
                    "allowed_telegram_user_ids",
                    "allowed_discord_user_ids",
                    "exclude_quiz_ids",
                ):
                    section[key] = _parse_id_list(value)
                    continue
                section[key] = value

        merge_section("moodle", body.moodle)
        merge_section("agents", body.agents)
        merge_section("solver", body.solver)
        merge_section("monitor", body.monitor)
        merge_section("remote", body.remote)
        merge_section("chat_bot", body.chat_bot)

        _save_config(cfg_path, cfg)

        # Refresh managers that depend on URL / runs dir
        nonlocal run_mgr, chat_mgr
        moodle_base = (cfg.get("moodle", {}) or {}).get("url", "")
        runs_dir = _runs_dir(cfg, cfg_path)
        if run_mgr is not None:
            run_mgr.moodle_base_url = moodle_base.rstrip("/")
            run_mgr.runs_dir = runs_dir
        chat_debug = bool((cfg.get("chat_bot", {}) or {}).get("debug", False))
        chat_mgr = ChatBotManager(runs_dir, config_path=str(cfg_path), debug=chat_debug)

        return {"ok": True, "config": _public_config(cfg), "agents_catalog": _agent_catalog(cfg)}

    @app.post("/api/agents/toggle")
    async def api_toggle_agent(body: AgentToggleRequest) -> dict:
        key = body.id.strip()
        if not key:
            raise HTTPException(400, "Missing agent id")
        cfg = _load_config(cfg_path)
        agents = cfg.setdefault("agents", {})

        if key.startswith("deepseek::"):
            model = key.split("::", 1)[1]
            entries = agents.setdefault("deepseek_models", [])
            if not _set_model_list_enabled(entries, model, body.enabled):
                entries.append({"model": model, "enabled": bool(body.enabled)})
        elif key.startswith("openrouter::"):
            model = key.split("::", 1)[1]
            entries = agents.setdefault("openrouter_models", [])
            if not _set_model_list_enabled(entries, model, body.enabled):
                entries.append({"model": model, "enabled": bool(body.enabled)})
        else:
            agents.setdefault("enabled", {})
            agents["enabled"][key] = bool(body.enabled)

        _save_config(cfg_path, cfg)
        return {"ok": True, "agents_catalog": _agent_catalog(cfg)}

    # ---- actions ---------------------------------------------------------

    @app.post("/api/solve")
    async def api_solve(body: SolveRequest) -> dict:
        nonlocal active_solve_log
        _ensure_managers()
        assert run_mgr is not None
        if run_mgr.is_busy():
            raise HTTPException(409, "A solve is already running")
        vid = parse_view_id_or_url(body.view)
        if not vid:
            raise HTTPException(400, "Could not parse quiz view id / URL")

        async def _run() -> None:
            nonlocal active_solve_log
            try:
                result = await run_mgr.run_solve(vid)
                active_solve_log = result.log_path
            except Exception:
                pass

        asyncio.create_task(_run())
        return {"ok": True, "view_id": vid}

    @app.post("/api/watch/start")
    async def api_watch_start() -> dict:
        _ensure_managers()
        assert watch_mgr is not None
        st = await watch_mgr.start_watch(started_by="gui")
        return {"ok": True, "status": st.__dict__}

    @app.post("/api/watch/stop")
    async def api_watch_stop() -> dict:
        _ensure_managers()
        assert watch_mgr is not None
        st = await watch_mgr.stop_watch()
        return {"ok": True, "status": st.__dict__}

    @app.post("/api/bots/start")
    async def api_bots_start() -> dict:
        _ensure_managers()
        assert bots_mgr is not None
        st = await bots_mgr.start()
        return {"ok": True, "status": st.__dict__}

    @app.post("/api/bots/stop")
    async def api_bots_stop() -> dict:
        _ensure_managers()
        assert bots_mgr is not None
        st = await bots_mgr.stop()
        return {"ok": True, "status": st.__dict__}

    @app.post("/api/chat/start")
    async def api_chat_start() -> dict:
        _ensure_managers()
        assert chat_mgr is not None
        st = await chat_mgr.start()
        return {"ok": True, "status": st.__dict__}

    @app.post("/api/chat/stop")
    async def api_chat_stop() -> dict:
        _ensure_managers()
        assert chat_mgr is not None
        st = await chat_mgr.stop()
        return {"ok": True, "status": st.__dict__}

    # ---- logs / history --------------------------------------------------

    @app.get("/api/logs")
    async def api_logs() -> dict:
        cfg = _load_config(cfg_path)
        return {"logs": _list_run_logs(_runs_dir(cfg, cfg_path))}

    @app.get("/api/logs/content")
    async def api_log_content(name: str, tail: int = 400) -> dict:
        cfg = _load_config(cfg_path)
        runs_dir = _runs_dir(cfg, cfg_path)
        path = (runs_dir / Path(name).name).resolve()
        if not str(path).startswith(str(runs_dir.resolve())):
            raise HTTPException(400, "Invalid log path")
        if not path.exists():
            raise HTTPException(404, "Log not found")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {
            "name": path.name,
            "lines": lines[-max(50, min(tail, 2000)) :],
            "total_lines": len(lines),
        }

    @app.get("/api/logs/stream")
    async def api_log_stream(name: str = "", source: str = "watch") -> StreamingResponse:
        """SSE-ish plain text stream of a live log file."""
        _ensure_managers()
        assert watch_mgr and bots_mgr and chat_mgr
        cfg = _load_config(cfg_path)
        runs_dir = _runs_dir(cfg, cfg_path)

        if name:
            path = runs_dir / Path(name).name
        elif source == "bots":
            path = Path(bots_mgr.status().log_path or "")
        elif source == "chat":
            path = Path(chat_mgr.status().log_path or "")
        elif source == "solve" and active_solve_log:
            path = Path(active_solve_log)
        else:
            path = Path(watch_mgr.status().log_path or "")

        async def _gen():
            if not path or not str(path):
                yield b"No log file yet. Start a job to create one.\n"
                return
            # Wait briefly for file creation
            for _ in range(40):
                if path.exists():
                    break
                await asyncio.sleep(0.25)
            if not path.exists():
                yield b"Waiting for log file...\n"
                return
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                try:
                    f.seek(max(0, path.stat().st_size - 12000))
                except Exception:
                    pass
                while True:
                    line = f.readline()
                    if line:
                        yield line.encode("utf-8", errors="ignore")
                    else:
                        await asyncio.sleep(0.4)

        return StreamingResponse(_gen(), media_type="text/plain; charset=utf-8")

    @app.get("/api/history")
    async def api_history() -> dict:
        cfg = _load_config(cfg_path)
        return {"attempts": _list_attempt_history(_logs_dir(cfg, cfg_path))}

    @app.get("/api/history/{attempt_id}")
    async def api_history_detail(attempt_id: str) -> dict:
        cfg = _load_config(cfg_path)
        logs_dir = _logs_dir(cfg, cfg_path)
        folder = (logs_dir / Path(attempt_id).name).resolve()
        if not str(folder).startswith(str(logs_dir.resolve())) or not folder.is_dir():
            raise HTTPException(404, "Attempt not found")

        summary = {}
        summary_path = folder / "attempt_summary.json"
        summary_txt = ""
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
        txt_path = folder / "attempt_summary.txt"
        if txt_path.exists():
            summary_txt = txt_path.read_text(encoding="utf-8", errors="replace")

        questions = []
        for qdir in sorted(folder.iterdir()):
            if not (qdir.is_dir() and qdir.name.startswith("q")):
                continue
            entry: dict[str, Any] = {"id": qdir.name, "files": []}
            for f in sorted(qdir.iterdir()):
                if f.is_file():
                    entry["files"].append(f.name)
            vote = qdir / "vote_result.json"
            if vote.exists():
                try:
                    entry["vote"] = json.loads(vote.read_text(encoding="utf-8"))
                except Exception:
                    pass
            questions.append(entry)

        return {
            "id": folder.name,
            "summary": summary,
            "summary_text": summary_txt,
            "questions": questions,
        }

    return app
