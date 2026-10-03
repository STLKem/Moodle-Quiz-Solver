from __future__ import annotations

import asyncio
import contextlib

import discord
from pathlib import Path
from typing import Iterable

from .runner import RunManager, WatchManager, parse_view_id_or_url


def _allowed(allowed_ids: Iterable[int], user_id: int | None) -> bool:
    if user_id is None:
        return False
    allowed = set(int(x) for x in allowed_ids)
    return user_id in allowed


async def start_discord_bot(
    token: str,
    allowed_user_ids: list[int],
    runs_dir: Path,
    moodle_base_url: str,
    *,
    run_mgr: RunManager | None = None,
    watch_mgr: WatchManager | None = None,
) -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    run_mgr = run_mgr or RunManager(runs_dir, moodle_base_url=moodle_base_url, config_path="config.yaml")
    watch_mgr = watch_mgr or WatchManager(runs_dir, config_path="config.yaml")

    @client.event
    async def on_ready():
        print(f"Discord bot logged in as {client.user}")
        print("Discord bot ready. DM me a number from view.php?id=XXXX or type 'id' / 'ping'.")
        try:
            allowed_preview = ", ".join(str(int(x)) for x in allowed_user_ids[:10])
            print(f"Allowed Discord IDs (count={len(allowed_user_ids)}): {allowed_preview}")
        except Exception:
            print(f"Allowed Discord IDs (count={len(allowed_user_ids)}): <unprintable>")

    @client.event
    async def on_message(message: discord.Message):
        try:
            if message.author == client.user:
                return

            uid = getattr(message.author, "id", None)
            content = (message.content or "").strip()
            cmd = content.lower()

            if cmd in ("ping",):
                await message.channel.send("pong")
                return
            if cmd in ("id", "myid"):
                is_allowed = _allowed(allowed_user_ids, uid)
                await message.channel.send(
                    f"Your Discord user id: {uid}\n"
                    f"Allowed right now: {is_allowed}\n"
                    f"Config list: remote.allowed_discord_user_ids (count={len(list(allowed_user_ids))})"
                )
                return
            if cmd in ("help",):
                await message.channel.send(
                    "Send a number from view.php?id=XXXX (e.g. 589608) or paste the full URL.\n"
                    "Commands: id, ping, start, stop, status"
                )
                return

            if not _allowed(allowed_user_ids, uid):
                if isinstance(message.channel, discord.DMChannel):
                    await message.channel.send(
                        f"Access denied for user id {uid}.\n"
                        f"Allowed list size: {len(list(allowed_user_ids))}\n"
                        f"Add it to config.yaml → remote.allowed_discord_user_ids, then restart: python main.py remote-bots"
                    )
                return

            if cmd in ("status",):
                st = watch_mgr.status()
                if st.running:
                    await message.channel.send(
                        f"watch-attempts: RUNNING\npid={st.pid}\nstarted_at={st.started_at}\nstarted_by={st.started_by}\nlog={st.log_path}"
                    )
                else:
                    await message.channel.send("watch-attempts: STOPPED")
                return

            if cmd in ("start",):
                async def _send(msg: str) -> None:
                    try:
                        await message.channel.send(msg)
                    except Exception:
                        pass

                st = await watch_mgr.start_watch(started_by=f"discord:{uid}", on_update=_send)
                if st.running:
                    await message.channel.send(f"watch-attempts started.\npid={st.pid}\nlog={st.log_path}")
                else:
                    await message.channel.send("Failed to start watch-attempts.")
                return

            if cmd in ("stop",):
                st = await watch_mgr.stop_watch()
                await message.channel.send("watch-attempts stopped." if not st.running else "Failed to stop watch-attempts.")
                return

            if not content:
                await message.channel.send(
                    "I can't see message text. Enable MESSAGE CONTENT INTENT for the bot in Discord Developer Portal."
                )
                return

            view_id = parse_view_id_or_url(content)
            if not view_id:
                await message.channel.send("Send only the number from view.php?id=XXXX or paste the full URL.")
                return

            if run_mgr.is_busy():
                await message.channel.send("Busy: a solve is already running. Wait for it to finish.")
                return

            await message.channel.send(f"Starting solve for view id {view_id}...")
            res = await run_mgr.run_solve(view_id)
            await message.channel.send(
                f"Finished view id {view_id}\n"
                f"Exit code: {res.exit_code}\n"
                f"Log: {res.log_path}"
            )
        except Exception as exc:
            try:
                await message.channel.send(f"Discord bot error: {exc}")
            except Exception:
                pass

    try:
        await client.start(token)
    except discord.LoginFailure as exc:
        print(
            f"[discord] Login failed: {exc}\n"
            "[discord] Fix remote.discord_bot_token in config.yaml "
            "(Bot → Token in Discord Developer Portal), or remove the token to skip Discord.",
            flush=True,
        )
        return
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(Exception):
            await client.close()

