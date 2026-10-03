from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from moodle.api import MoodleAPIError


class MoodleChatError(Exception):
    """Raised on Moodle chat/messaging API errors."""


@dataclass
class ChatMessage:
    id: int
    useridfrom: int
    text: str
    timecreated: int


@dataclass
class Conversation:
    id: int
    name: str
    type: int  # 1=private, 2=group, 3=self


class MoodleChatClient:
    """
    Wrapper for Moodle chat messaging.

    Uses core_message (personal messaging) API primarily.
    Falls back to mod_chat (Chat activity) if needed.
    """

    def __init__(self, api) -> None:
        self._api = api
        self._client = api._client
        self._token = api.token
        self._base_url = api.base_url
        self._userid: Optional[int] = None

    async def _ensure_userid(self) -> int:
        if self._userid is None:
            info = await self._api._call("core_webservice_get_site_info")
            self._userid = int(info["userid"])
        return self._userid

    # ------------------------------------------------------------------
    # core_message (personal messaging)
    # ------------------------------------------------------------------

    async def get_conversations(self) -> list[Conversation]:
        userid = await self._ensure_userid()
        data = await self._api._call(
            "core_message_get_conversations",
            userid=userid,
            limitfrom=0,
            limitnum=50,
        )
        convs: list[Conversation] = []
        for c in data.get("conversations", []):
            convs.append(
                Conversation(
                    id=int(c["id"]),
                    name=c.get("name", ""),
                    type=int(c.get("type", 1)),
                )
            )
        return convs

    async def _get_self_conversation_messages(
        self, userid: int, limitnum: int
    ) -> list[dict]:
        """Fetch messages via core_message_get_self_conversation (userid only)."""
        data = await self._api._call(
            "core_message_get_self_conversation",
            userid=userid,
            messagelimit=limitnum,
            messageoffset=0,
            newestmessagesfirst=1,
        )
        if not isinstance(data, dict):
            return []
        messages = data.get("messages")
        if isinstance(messages, list):
            return messages
        conv = data.get("conversation")
        if isinstance(conv, dict) and isinstance(conv.get("messages"), list):
            return conv["messages"]
        return []

    async def _try_get_messages(
        self, userid: int, conversation_id: int, limitfrom: int, limitnum: int
    ) -> list[dict]:
        errors: list[str] = []

        # Moodle WS expects `convid`, not conversationid/conv_id.
        param_sets = (
            {
                "currentuserid": userid,
                "convid": conversation_id,
                "limitfrom": limitfrom,
                "limitnum": limitnum,
                "newest": 1,
            },
            {
                "currentuserid": userid,
                "convid": conversation_id,
                "limitfrom": limitfrom,
                "limitnum": limitnum,
            },
            {"conversationid": conversation_id, "currentuserid": userid},
        )
        for extra in param_sets:
            params = {**extra}
            if "limitfrom" not in params:
                params["limitfrom"] = limitfrom
                params["limitnum"] = limitnum
            try:
                data = await self._api._call(
                    "core_message_get_conversation_messages",
                    **params,
                )
                raw = data.get("messages", []) if isinstance(data, dict) else []
                if raw is not None:
                    return raw
            except MoodleAPIError as exc:
                errors.append(str(exc)[:120])
                continue

        # Self-conversation fallback (type 3)
        self_conv = await self.find_self_conversation()
        if self_conv is not None and int(self_conv.id) == int(conversation_id):
            try:
                return await self._get_self_conversation_messages(userid, limitnum)
            except MoodleAPIError as exc:
                errors.append(f"get_self_conversation: {str(exc)[:120]}")

        raise MoodleChatError(
            f"Failed to get messages for conversation {conversation_id}: "
            f"{' / '.join(errors)}"
        )

    async def get_messages(
        self, conversation_id: int, limitfrom: int = 0, limitnum: int = 50
    ) -> list[ChatMessage]:
        userid = await self._ensure_userid()
        raw_messages = await self._try_get_messages(userid, conversation_id, limitfrom, limitnum)
        messages: list[ChatMessage] = []
        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            messages.append(
                ChatMessage(
                    id=int(m["id"]),
                    useridfrom=int(m.get("useridfrom", 0)),
                    text=m.get("text", ""),
                    timecreated=int(m.get("timecreated", 0)),
                )
            )
        return messages

    async def send_message(self, conversation_id: int, text: str) -> int:
        """Send a message; returns the created Moodle message id."""
        last_error: Optional[MoodleAPIError] = None
        for params in (
            {"conversationid": conversation_id},
            {"conv_id": conversation_id},
        ):
            try:
                result = await self._api._call(
                    "core_message_send_messages_to_conversation",
                    **params,
                    **{"messages[0][text]": text, "messages[0][textformat]": 1},
                )
                if isinstance(result, list) and result:
                    return int(result[0].get("id", 0))
                if isinstance(result, dict) and result.get("id"):
                    return int(result["id"])
                return 0
            except MoodleAPIError as exc:
                last_error = exc
                continue
        raise MoodleChatError(
            f"Failed to send message to conversation {conversation_id}"
        ) from last_error

    async def find_self_conversation(self) -> Optional[Conversation]:
        userid = await self._ensure_userid()
        data = await self._api._call(
            "core_message_get_self_conversation",
            userid=userid,
        )
        if data and "id" in data:
            return Conversation(
                id=int(data["id"]),
                name=data.get("name", ""),
                type=3,
            )
        return None

    # ------------------------------------------------------------------
    # mod_chat (Chat activity) — fallback
    # ------------------------------------------------------------------

    async def _inspect_course_module(self, cmid: int) -> Optional[dict]:
        """
        Try `core_course_get_course_module` for the given id.
        Returns the `cm` dict on success, or None if the id is not a valid cmid.
        """
        try:
            data = await self._api._call(
                "core_course_get_course_module", cmid=int(cmid)
            )
        except MoodleAPIError:
            return None
        if not isinstance(data, dict):
            return None
        cm = data.get("cm")
        return cm if isinstance(cm, dict) else None

    async def _discover_chat_activities(self) -> list[tuple[str, str, int]]:
        """
        Walk all enrolled courses and return every available Chat activity as
        a (course_shortname, chat_name, chat_instance_id) tuple.
        """
        results: list[tuple[str, str, int]] = []
        try:
            info = await self._api._call("core_webservice_get_site_info")
            uid = info["userid"]
            courses = await self._api._call(
                "core_enrol_get_users_courses", userid=uid
            )
        except MoodleAPIError:
            return results
        for c in courses or []:
            try:
                raw = await self._api._call(
                    "mod_chat_get_chats_by_courses",
                    **{"courseids[0]": str(c["id"])},
                )
            except MoodleAPIError:
                continue
            for ch in raw.get("chats", []) or []:
                try:
                    results.append((
                        c.get("shortname", "") or str(c.get("id", "")),
                        ch.get("name", "") or f"Chat {ch.get('id')}",
                        int(ch["id"]),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
        return results

    async def mod_chat_login(self, chat_id: int) -> str:
        # 1) try as a real chat instance id
        try:
            data = await self._api._call("mod_chat_login_user", chatid=chat_id)
        except MoodleAPIError as exc:
            msg = str(exc).lower()
            looks_like_missing = (
                "can't find data record" in msg
                or "cannot find data record" in msg
                or "invalid chat id" in msg
                or "invalid record" in msg
            )
            if not looks_like_missing:
                raise

            # 2) maybe chat_id is actually a course module id (?id= in URL)
            cm = await self._inspect_course_module(chat_id)
            if cm is not None:
                modname = (cm.get("modname") or "").lower()
                instance = cm.get("instance")
                if modname == "chat" and instance:
                    print(
                        f"[chat-bot] chat_id={chat_id} looked like a cmid; "
                        f"resolved to chat instance id={instance}"
                    )
                    data = await self._api._call(
                        "mod_chat_login_user", chatid=int(instance)
                    )
                    chatsid = data.get("chatsid") or data.get("chatSid") or ""
                    if not chatsid:
                        raise MoodleChatError(
                            f"mod_chat login failed for chat {chat_id} "
                            f"(resolved instance {instance})"
                        )
                    return str(chatsid)

                # cmid is valid but for a different module
                wrong_kind = (
                    f"chat_id={chat_id} is a course module id, but the "
                    f"activity is `{modname or 'unknown'}`"
                    + (f" (\"{cm.get('name')}\")" if cm.get("name") else "")
                    + ", not a Chat (mod_chat)."
                )
            else:
                wrong_kind = (
                    f"chat_id={chat_id} is not a valid Chat activity id, "
                    "and is not a recognizable course module id either."
                )

            # 3) maybe the user put a personal conversation id in chat_id
            self_conv = await self.find_self_conversation()
            if self_conv is not None and int(self_conv.id) == int(chat_id):
                raise MoodleChatError(
                    f"chat_id={chat_id} is your Self-conversation "
                    "(personal messages), not a course Chat activity.\n"
                    "In config.yaml use:\n"
                    f"  conversation_id: {chat_id}\n"
                    "and remove `chat_id`, or run:\n"
                    f"  python main.py chat-bot --conv-id {chat_id}"
                ) from exc

            # 4) auto-list available chats so the user can pick the right id
            available = await self._discover_chat_activities()
            if available:
                lines = [
                    f"  - {course} :: {name}  ->  chat_id: {cid}"
                    for course, name, cid in sorted(available)
                ]
                hint = (
                    "Available Chat activities you can use in config.yaml "
                    "(`chat_bot.chat_id`):\n" + "\n".join(lines)
                )
            else:
                hint = (
                    "No Chat (mod_chat) activities found in your enrolled "
                    "courses. Use `--conv-id <ID>` for personal messaging "
                    "instead, or run `python main.py list-chats`."
                )
            raise MoodleChatError(f"{wrong_kind}\n{hint}") from exc

        chatsid = data.get("chatsid") or data.get("chatSid") or ""
        if not chatsid:
            raise MoodleChatError(f"mod_chat login failed for chat {chat_id}")
        return str(chatsid)

    async def mod_chat_get_messages(
        self, chat_sid: str, last_time: int = 0
    ) -> list[ChatMessage]:
        data = await self._api._call(
            "mod_chat_get_chat_latest_messages",
            chatsid=chat_sid,
            chat_last_time=last_time,
        )
        raw = []
        if isinstance(data, dict):
            raw = data.get("messages", [])
        elif isinstance(data, list):
            raw = data
        messages: list[ChatMessage] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            messages.append(
                ChatMessage(
                    id=int(m.get("id", 0)),
                    useridfrom=int(m.get("userid", 0)),
                    text=m.get("message", ""),
                    timecreated=int(m.get("timestamp", 0)),
                )
            )
        return messages

    async def mod_chat_send(self, chat_sid: str, text: str) -> None:
        await self._api._call(
            "mod_chat_send_chat_message",
            chatsid=chat_sid,
            messagetext=text,
        )
