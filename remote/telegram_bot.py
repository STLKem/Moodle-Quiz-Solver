from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Iterable

from telegram.error import NetworkError
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .runner import RunManager, WatchManager, parse_view_id_or_url


def _allowed(allowed_ids: Iterable[int], user_id: int | None) -> bool:
    if user_id is None:
        return False
    allowed = set(int(x) for x in allowed_ids)
    return user_id in allowed


async def start_telegram_bot(
    token: str,
    allowed_user_ids: list[int],
    runs_dir: Path,
    moodle_base_url: str,
    *,
    run_mgr: RunManager | None = None,
    watch_mgr: WatchManager | None = None,
) -> None:
    run_mgr = run_mgr or RunManager(runs_dir, moodle_base_url=moodle_base_url, config_path="config.yaml")
    watch_mgr = watch_mgr or WatchManager(runs_dir, config_path="config.yaml")

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id if update.effective_user else None
        text = (update.message.text or "").strip() if update.message else ""
        cmd = text.lower()

        if cmd in ("ping",):
            await update.message.reply_text("pong")
            return
        if cmd in ("id", "myid"):
            await update.message.reply_text(
                f"Your Telegram user id: {uid}\n"
                f"Add it to config.yaml → remote.allowed_telegram_user_ids"
            )
            return
        if cmd in ("help",):
            await update.message.reply_text(
                "Send the number from view.php?id=XXXX (or paste the full URL).\n"
                "Commands: id, ping, start, stop, status"
            )
            return

        if not _allowed(allowed_user_ids, uid):
            await update.message.reply_text(
                f"Access denied for user id {uid}. Add it to config.yaml → remote.allowed_telegram_user_ids"
            )
            return

        if cmd in ("status",):
            st = watch_mgr.status()
            if st.running:
                await update.message.reply_text(
                    f"watch-attempts: RUNNING\npid={st.pid}\nstarted_at={st.started_at}\nstarted_by={st.started_by}\nlog={st.log_path}"
                )
            else:
                await update.message.reply_text("watch-attempts: STOPPED")
            return

        if cmd in ("start",):
            async def _send(msg: str) -> None:
                try:
                    await update.message.reply_text(msg)
                except Exception:
                    pass

            st = await watch_mgr.start_watch(started_by=f"telegram:{uid}", on_update=_send)
            if st.running:
                await update.message.reply_text(
                    f"watch-attempts started.\npid={st.pid}\nlog={st.log_path}"
                )
            else:
                await update.message.reply_text("Failed to start watch-attempts.")
            return

        if cmd in ("stop",):
            st = await watch_mgr.stop_watch()
            await update.message.reply_text("watch-attempts stopped." if not st.running else "Failed to stop watch-attempts.")
            return

        view_id = parse_view_id_or_url(text)
        if not view_id:
            await update.message.reply_text(
                "Send the number from view.php?id=XXXX (or paste the full URL)."
            )
            return

        if run_mgr.is_busy():
            await update.message.reply_text("Busy: a solve is already running. Wait for it to finish.")
            return

        await update.message.reply_text(f"Starting solve for view id {view_id}...")
        try:
            res = await run_mgr.run_solve(view_id)
            await update.message.reply_text(
                f"Finished view id {view_id}\n"
                f"Exit code: {res.exit_code}\n"
                f"Log: {res.log_path}"
            )
        except Exception as exc:
            await update.message.reply_text(f"Error: {exc}")

    # Resilient polling loop: Telegram polling can occasionally throw transient
    # httpx/httpcore read errors; we reconnect with backoff instead of crashing
    # the whole remote-bots process.
    async def _shutdown_app(app: Application) -> None:
        updater = app.updater
        if updater is not None and getattr(updater, "running", False):
            with contextlib.suppress(Exception):
                await updater.stop()
        if getattr(app, "running", False):
            with contextlib.suppress(Exception):
                await app.stop()
        with contextlib.suppress(Exception):
            await app.shutdown()

    backoffs = [1, 2, 5, 10, 20, 30]
    attempt = 0
    while True:
        app = Application.builder().token(token).build()
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
        cancelled = False
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
            attempt = 0
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled = True
        except NetworkError as exc:
            delay = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            print(f"[telegram] network error: {exc} — reconnecting in {delay}s")
            await asyncio.sleep(delay)
        except Exception as exc:
            delay = backoffs[min(attempt, len(backoffs) - 1)]
            attempt += 1
            print(f"[telegram] error: {exc} — reconnecting in {delay}s")
            await asyncio.sleep(delay)
        finally:
            await _shutdown_app(app)
        if cancelled:
            return

