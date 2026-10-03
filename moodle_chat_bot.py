from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from typing import Optional

from agents.base import AgentAnswer, BaseAgent
from agents.registry import build_single_agent
from moodle.api import MoodleAPI
from moodle.chat_client import MoodleChatClient, Conversation


CHAT_SYSTEM_PROMPT = """\
You are a helpful AI assistant integrated into a Moodle chat.
Return ONLY a valid JSON object with no extra text:

{"answer": "your response text here", "confidence": 0.9, "reasoning": "brief reasoning"}
"""

DEFAULT_REPLY_RULES = """\
Answer rules (apply to the "answer" field only):
- Give ONLY the final answer. No greetings, no explanations, no extra sentences.
- If the message is a question or exercise, reply with the solution only.
- Multiple choice: output only the option letter (a/b/c) or the exact option word, nothing else.
- Fill-in-the-gap / complete the sentence: output only the missing word(s) or phrase.
- Translation or grammar drill: output only the required word or short phrase.
- Put any brief notes only in "reasoning", never in "answer".
"""


def _build_chat_prompt(chat_bot_cfg: dict, user_text: str) -> str:
    extra = (chat_bot_cfg.get("reply_rules") or DEFAULT_REPLY_RULES).strip()
    parts = [CHAT_SYSTEM_PROMPT.strip()]
    if extra:
        parts.append(extra)
    parts.append(f"User message:\n{user_text}")
    return "\n\n".join(parts)


def _chat_debug(chat_bot_cfg: dict) -> bool:
    return bool(chat_bot_cfg.get("debug")) or os.environ.get("CHAT_BOT_DEBUG") == "1"


def _log(msg: str, *, debug_only: bool = False, chat_bot_cfg: dict | None = None) -> None:
    if debug_only and chat_bot_cfg is not None and not _chat_debug(chat_bot_cfg):
        return
    print(msg, flush=True)


async def _debug_list_conversations(chat) -> None:
    print("[chat-bot] === Personal conversations (core_message) ===")
    convs = await chat.get_conversations()
    self_conv = await chat.find_self_conversation()
    type_names = {1: "private", 2: "group", 3: "self"}
    if self_conv:
        print(f"  SELF: id={self_conv.id}, name='{self_conv.name}'")
        try:
            msgs = await chat.get_messages(self_conv.id, limitnum=5)
            print(f"    messages: {len(msgs)}")
            for m in msgs[:3]:
                print(f"      [{m.id}] from={m.useridfrom}: {m.text[:80]}")
        except Exception as e:
            print(f"    error: {e}")
    for c in convs:
        skip = " (SELF)" if self_conv and c.id == self_conv.id else ""
        print(f"  id={c.id}, type={type_names.get(c.type, str(c.type))}, name='{c.name}'{skip}")
        try:
            msgs = await chat.get_messages(c.id, limitnum=3)
            print(f"    messages: {len(msgs)}")
            for m in msgs[:2]:
                print(f"      [{m.id}] from={m.useridfrom}: {m.text[:80]}")
        except Exception as e:
            print(f"    error: {e}")

    print("[chat-bot] === Chat activities (mod_chat) ===")
    try:
        from moodle.client import MoodleClient, ConnectionMode
        api = chat._api
        info = await api._call("core_webservice_get_site_info")
        uid = info["userid"]
        courses = await api._call("core_enrol_get_users_courses", userid=uid)
        for c in courses[:10]:
            try:
                raw = await api._call("mod_chat_get_chats_by_courses", **{"courseids[0]": c["id"]})
                for ch in raw.get("chats", []):
                    ch_id = ch["id"]
                    ch_name = ch.get("name", "")
                    print(f"  course={c.get('shortname','')}, name='{ch_name}', chat_id={ch_id}")
            except Exception as e:
                pass
    except Exception as e:
        print(f"  error listing chat activities: {e}")


async def run_chat_bot(
    moodle_cfg: dict,
    agent_cfg: dict,
    chat_bot_cfg: dict,
    conversation_id_override: Optional[int] = None,
    chat_id_override: Optional[int] = None,
) -> None:
    debug = _chat_debug(chat_bot_cfg)
    conv_id = conversation_id_override or chat_bot_cfg.get("conversation_id")
    chat_id = chat_id_override or chat_bot_cfg.get("chat_id")
    use_mod_chat = chat_id is not None
    poll_seconds = int(chat_bot_cfg.get("poll_seconds", 5))
    model_name = chat_bot_cfg.get("model", "")

    _log(f"[chat-bot] pid={os.getpid()} debug={debug}", chat_bot_cfg=chat_bot_cfg)
    _log(
        f"[chat-bot] config: model={model_name!r} conv_id={conv_id} chat_id={chat_id} "
        f"poll_seconds={poll_seconds}",
        chat_bot_cfg=chat_bot_cfg,
    )

    if not model_name:
        _log("[chat-bot] No model specified in config under chat_bot.model")
        return

    _log("[chat-bot] Connecting to Moodle...")
    try:
        async with MoodleAPI(
            moodle_cfg["url"],
            moodle_cfg["username"],
            moodle_cfg["password"],
        ) as api:
            chat = MoodleChatClient(api)

            chat_sid: Optional[str] = None
            if use_mod_chat:
                chat_id = int(chat_id)
                _log(f"[chat-bot] Using mod_chat (Chat activity) id={chat_id}")
                chat_sid = await chat.mod_chat_login(chat_id)
                _log(f"[chat-bot] Logged into chat, chat_sid={chat_sid}")
            elif conv_id is not None:
                conv_id = int(conv_id)
                _log(f"[chat-bot] Using core_message conversation_id={conv_id}")
            else:
                self_conv = await chat.find_self_conversation()
                if self_conv is not None:
                    conv_id = self_conv.id
                    _log(f"[chat-bot] Using auto-detected self-conversation (id={conv_id})")
                else:
                    _log("[chat-bot] No chat_id or conversation_id configured.")
                    _log("[chat-bot] Run 'python main.py list-chats' to find one.")
                    return

            _log(f"[chat-bot] Building agent '{model_name}'...")
            agent = build_single_agent(agent_cfg, model_name)
            _log(f"[chat-bot] Using agent: {agent.name} ({agent.model})")

            own_userid = await chat._ensure_userid()
            is_self_chat = False
            if conv_id is not None and chat_sid is None:
                self_conv = await chat.find_self_conversation()
                is_self_chat = (
                    self_conv is not None and int(self_conv.id) == int(conv_id)
                )
            _log(
                f"[chat-bot] own_userid={own_userid} is_self_chat={is_self_chat}",
                debug_only=True,
                chat_bot_cfg=chat_bot_cfg,
            )
            bot_started_at = int(time.time())
            bot_own_message_ids: set[int] = set()
            last_processed_id: int = 0
            last_timestamp: int = 0
            poll_count = 0

            def _is_user_message(msg) -> bool:
                if msg.id in bot_own_message_ids:
                    return False
                if msg.timecreated < bot_started_at:
                    return False
                if is_self_chat:
                    return msg.useridfrom == own_userid
                return msg.useridfrom != own_userid

            # Baseline: ignore all messages that already exist at startup.
            try:
                if chat_sid is not None:
                    baseline = await chat.mod_chat_get_messages(chat_sid, last_time=0)
                else:
                    baseline = await chat.get_messages(conv_id, limitnum=100)
                for msg in baseline:
                    bot_own_message_ids.add(msg.id)
                    if msg.id > last_processed_id:
                        last_processed_id = msg.id
                    if msg.timecreated > last_timestamp:
                        last_timestamp = msg.timecreated
                _log(
                    f"[chat-bot] Ignoring {len(baseline)} existing message(s); "
                    f"only new messages after {bot_started_at} will be answered."
                )
                if debug and baseline:
                    last = baseline[-1]
                    _log(
                        f"[chat-bot:debug] baseline last msg id={last.id} "
                        f"from={last.useridfrom} ts={last.timecreated}",
                        chat_bot_cfg=chat_bot_cfg,
                    )
            except Exception as exc:
                _log(f"[chat-bot] Baseline fetch warning: {exc}")
                if debug:
                    traceback.print_exc()

            _log(f"[chat-bot] Starting poll loop (every {poll_seconds}s)...")
            while True:
                poll_count += 1
                try:
                    if chat_sid is not None:
                        raw = await chat.mod_chat_get_messages(chat_sid, last_time=last_timestamp)
                    else:
                        raw = await chat.get_messages(conv_id, limitnum=50)
                    messages = sorted(raw, key=lambda m: m.id)
                except Exception as exc:
                    _log(f"[chat-bot] Poll error: {exc}")
                    if debug:
                        traceback.print_exc()
                    await asyncio.sleep(poll_seconds)
                    continue

                pending = 0
                skipped_old = 0
                skipped_not_user = 0
                skipped_empty = 0
                for msg in messages:
                    if msg.id <= last_processed_id:
                        skipped_old += 1
                        continue
                    if not _is_user_message(msg):
                        skipped_not_user += 1
                        continue
                    if not re.sub(r"<[^>]+>", "", msg.text).strip():
                        skipped_empty += 1
                        continue
                    pending += 1

                _log(
                    f"[chat-bot:debug] poll #{poll_count}: fetched={len(messages)} "
                    f"pending={pending} skipped_old={skipped_old} "
                    f"skipped_not_user={skipped_not_user} skipped_empty={skipped_empty} "
                    f"last_processed_id={last_processed_id} last_ts={last_timestamp}",
                    debug_only=True,
                    chat_bot_cfg=chat_bot_cfg,
                )

                if pending:
                    _log(
                        f"[chat-bot] Poll: {pending} new user message(s) "
                        f"(last_processed_id={last_processed_id})"
                    )

                new_max_id = last_processed_id
                new_last_ts = last_timestamp
                for msg in messages:
                    if msg.id <= last_processed_id:
                        continue
                    if msg.id > new_max_id:
                        new_max_id = msg.id
                    if msg.timecreated > new_last_ts:
                        new_last_ts = msg.timecreated

                    if not _is_user_message(msg):
                        _log(
                            f"[chat-bot:debug] skip msg id={msg.id} from={msg.useridfrom} "
                            f"(not user message)",
                            debug_only=True,
                            chat_bot_cfg=chat_bot_cfg,
                        )
                        continue

                    text = re.sub(r"<[^>]+>", "", msg.text)
                    if not text.strip():
                        continue

                    _log(f"[chat-bot] >>> NEW MESSAGE (id={msg.id}): {text[:120]}")
                    prompt = _build_chat_prompt(chat_bot_cfg, text)
                    try:
                        answer: AgentAnswer = await asyncio.wait_for(
                            agent.ask(prompt, temperature=0.3),
                            timeout=60.0,
                        )
                    except asyncio.TimeoutError:
                        _log(f"[chat-bot] Agent timed out on message {msg.id}")
                        continue
                    except UnicodeEncodeError as exc:
                        _log(f"[chat-bot] Encoding error on message {msg.id}: {exc}")
                        safe = text.encode("ascii", errors="replace").decode("ascii")
                        prompt = _build_chat_prompt(chat_bot_cfg, safe)
                        try:
                            answer: AgentAnswer = await asyncio.wait_for(
                                agent.ask(prompt, temperature=0.3),
                                timeout=60.0,
                            )
                        except Exception as exc2:
                            _log(f"[chat-bot] Agent error on retry: {exc2}")
                            continue
                    except Exception as exc:
                        _log(f"[chat-bot] Agent error on message {msg.id}: {exc}")
                        if debug:
                            traceback.print_exc()
                        continue

                    if answer.error:
                        _log(f"[chat-bot] Agent error: {answer.error}")
                        continue

                    response_text = answer.answer
                    _log(f"[chat-bot] Agent raw answer: {response_text!r}")
                    if isinstance(response_text, list):
                        response_text = "\n".join(response_text)
                    elif isinstance(response_text, dict):
                        response_text = str(response_text)
                    response_text = str(response_text).strip()
                    if not response_text:
                        _log(f"[chat-bot] Empty answer for message {msg.id}")
                        continue

                    _log(f"[chat-bot] >>> SENDING RESPONSE: {response_text[:120]}")
                    try:
                        if chat_sid is not None:
                            await chat.mod_chat_send(chat_sid, response_text)
                            sent_id = 0
                        else:
                            sent_id = await chat.send_message(conv_id, response_text)
                        if sent_id:
                            bot_own_message_ids.add(sent_id)
                            if sent_id > new_max_id:
                                new_max_id = sent_id

                        # In a self-conversation the bot's reply has the SAME
                        # useridfrom as the user, so we MUST record its id to
                        # avoid replying to our own answer. send_message often
                        # returns 0, so re-fetch and match our reply by text.
                        if chat_sid is None and not sent_id:
                            try:
                                after = await chat.get_messages(conv_id, limitnum=10)
                                target = response_text.strip()
                                for m in sorted(after, key=lambda x: x.id, reverse=True):
                                    if m.useridfrom != own_userid:
                                        continue
                                    clean = re.sub(r"<[^>]+>", "", m.text).strip()
                                    if clean == target:
                                        bot_own_message_ids.add(m.id)
                                        if m.id > new_max_id:
                                            new_max_id = m.id
                                        _log(
                                            f"[chat-bot:debug] tracked own reply id={m.id}",
                                            debug_only=True,
                                            chat_bot_cfg=chat_bot_cfg,
                                        )
                                        break
                            except Exception as exc2:
                                _log(
                                    f"[chat-bot] Warning: could not track own reply id: {exc2}"
                                )
                        _log(f"[chat-bot] Response sent OK (sent_id={sent_id})")
                    except Exception as exc:
                        _log(f"[chat-bot] Send error: {exc}")
                        if debug:
                            traceback.print_exc()

                if new_max_id > last_processed_id:
                    last_processed_id = new_max_id
                if new_last_ts > last_timestamp:
                    last_timestamp = new_last_ts
                await asyncio.sleep(poll_seconds)
    except Exception as exc:
        _log(f"[chat-bot] FATAL: {exc}")
        traceback.print_exc()
        raise
