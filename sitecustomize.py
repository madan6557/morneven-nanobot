"""Runtime compatibility patches for Morneven Nanobot."""

from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMMAND_TARGET_RE = re.compile(r"^/[A-Za-z0-9_-]+@([A-Za-z0-9_]+)(?=$|\s)")
LEADING_MENTION_RE = re.compile(r"^@([A-Za-z0-9_]+)(?=$|\s)")
COMMAND_TARGETS_RE = re.compile(r"(?:^|\s)/[A-Za-z0-9_-]+@([A-Za-z0-9_]+)(?=$|\s)")
MENTIONS_RE = re.compile(r"@([A-Za-z0-9_]+)(?=$|\s|[.,!?;:])")


def _normalize_username(value: Any) -> str:
    if not value:
        return ""
    return str(value).strip().lstrip("@").lower()


def _env_username(name: str) -> str:
    return _normalize_username(os.environ.get(name, ""))


def _env_usernames(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {_normalize_username(item) for item in raw.split(",") if _normalize_username(item)}


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return str(text).strip()


def _routing_debug_enabled() -> bool:
    value = os.environ.get("MORNEVEN_TELEGRAM_ROUTING_DEBUG", "1").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _message_chat_id(message: Any) -> str:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return str(chat_id or "")


def _message_thread_id(message: Any) -> str:
    thread_id = getattr(message, "message_thread_id", None)
    return str(thread_id or "")


def _debug_routing(message: Any, username: str, targets: set[str], decision: str, reason: str) -> None:
    if not _routing_debug_enabled():
        return
    if not targets:
        return
    ordered_targets = ",".join(sorted(targets))
    print(
        "[morneven-telegram-routing] "
        f"bot={username or '-'} targets={ordered_targets or '-'} decision={decision} "
        f"reason={reason} chat={_message_chat_id(message) or '-'} "
        f"thread={_message_thread_id(message) or '-'} "
        f"token={os.environ.get('MORNEVEN_TELEGRAM_TOKEN_FINGERPRINT', '-') or '-'}",
        flush=True,
    )


def _debug_ingress(message: Any, username: str, targets: set[str], stage: str) -> None:
    if not _routing_debug_enabled():
        return
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", "") or "-"
    if chat_type == "private":
        return
    ordered_targets = ",".join(sorted(targets)) or "-"
    reply_to = getattr(message, "reply_to_message", None)
    reply_user = getattr(reply_to, "from_user", None)
    reply_username = _normalize_username(getattr(reply_user, "username", None))
    print(
        "[morneven-telegram-ingress] "
        f"stage={stage} bot={username or '-'} targets={ordered_targets} "
        f"chat={_message_chat_id(message) or '-'} chat_type={chat_type} "
        f"thread={_message_thread_id(message) or '-'} "
        f"text={'1' if getattr(message, 'text', None) else '0'} "
        f"caption={'1' if getattr(message, 'caption', None) else '0'} "
        f"reply_to={reply_username or '-'} "
        f"token={os.environ.get('MORNEVEN_TELEGRAM_TOKEN_FINGERPRINT', '-') or '-'}",
        flush=True,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic_id(value: Any) -> str:
    if value is None:
        return "main"
    text = str(value).strip()
    if not text or text in {"0", "1"} or text.lower() == "main":
        return "main"
    return text


def _topics_path() -> Path | None:
    raw = os.environ.get("MORNEVEN_TELEGRAM_TOPICS_PATH", "").strip()
    return Path(raw) if raw else None


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        return


def _message_topic_title(message: Any, topic: str) -> str:
    for attr in ("forum_topic_created", "forum_topic_edited"):
        event = getattr(message, attr, None)
        name = getattr(event, "name", None)
        if name:
            return str(name)
    return "Main topic" if topic == "main" else f"Topic {topic}"


def _record_topic(message: Any) -> None:
    path = _topics_path()
    if not path:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return
    chat_type = getattr(chat, "type", None)
    if chat_type == "private":
        return
    now = _now_iso()
    topic = _topic_id(getattr(message, "message_thread_id", None))
    registry = _read_json_file(path)
    groups = registry.get("groups") if isinstance(registry.get("groups"), list) else []
    chat_id_text = str(chat_id)
    group = next((item for item in groups if isinstance(item, dict) and str(item.get("chatId")) == chat_id_text), None)
    if not group:
        group = {
            "chatId": chat_id_text,
            "title": str(getattr(chat, "title", "") or ""),
            "isForum": bool(getattr(chat, "is_forum", False)),
            "lastSeenAt": now,
            "source": "observed",
            "topics": [],
        }
        groups.append(group)
    else:
        group["title"] = str(getattr(chat, "title", "") or group.get("title") or "")
        group["isForum"] = bool(getattr(chat, "is_forum", group.get("isForum", False)))
        group["lastSeenAt"] = now
    topics = group.get("topics") if isinstance(group.get("topics"), list) else []
    topic_entry = next((item for item in topics if isinstance(item, dict) and _topic_id(item.get("messageThreadId")) == topic), None)
    if not topic_entry:
        topics.append({
            "messageThreadId": topic,
            "title": _message_topic_title(message, topic),
            "lastSeenAt": now,
            "source": "observed",
        })
    else:
        topic_entry["title"] = topic_entry.get("title") or _message_topic_title(message, topic)
        topic_entry["lastSeenAt"] = now
        topic_entry["source"] = "observed" if topic_entry.get("source") != "manual" else "manual"
    group["topics"] = topics
    registry["groups"] = groups
    _write_json_file(path, registry)


def _runtime_config() -> dict[str, Any]:
    raw = os.environ.get("MORNEVEN_NANOBOT_CONFIG_PATH", "").strip()
    if raw:
        config = _read_json_file(Path(raw))
        if config:
            return config
    try:
        from nanobot.config.loader import load_config

        config = load_config()
        if hasattr(config, "model_dump"):
            payload = config.model_dump(by_alias=True)
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def _telegram_lock_config() -> dict[str, Any]:
    config = _runtime_config()
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    telegram = channels.get("telegram") if isinstance(channels.get("telegram"), dict) else {}
    lock = telegram.get("topicLock") if isinstance(telegram.get("topicLock"), dict) else {}
    return lock


def _topic_lock_has_group_rules(lock: dict[str, Any]) -> bool:
    groups = lock.get("groups") if isinstance(lock.get("groups"), list) else []
    for group in groups:
        if not isinstance(group, dict):
            continue
        allowed = group.get("allowedTopicIds") if isinstance(group.get("allowedTopicIds"), list) else []
        primary = _topic_id(group.get("primaryTopicId"))
        if group.get("allowMainTopic") is False or len(allowed) > 0 or primary != "main":
            return True
    return False


def _topic_lock_enabled(lock: dict[str, Any]) -> bool:
    return lock.get("enabled") is True or _topic_lock_has_group_rules(lock)


def _topic_lock_group(chat_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    lock = _telegram_lock_config()
    groups = lock.get("groups") if isinstance(lock.get("groups"), list) else []
    group = next((item for item in groups if isinstance(item, dict) and str(item.get("chatId")) == str(chat_id)), None)
    return lock, group


def _group_topic_allows(group: dict[str, Any], thread_id: str) -> bool:
    if thread_id == "main":
        return group.get("allowMainTopic") is not False
    allowed = group.get("allowedTopicIds") if isinstance(group.get("allowedTopicIds"), list) else []
    return thread_id in {str(item).strip() for item in allowed}


def _group_primary_topic(group: dict[str, Any]) -> str:
    primary = _topic_id(group.get("primaryTopicId"))
    if _group_topic_allows(group, primary):
        return primary
    allowed = group.get("allowedTopicIds") if isinstance(group.get("allowedTopicIds"), list) else []
    for item in allowed:
        topic = _topic_id(item)
        if topic != "main" and _group_topic_allows(group, topic):
            return topic
    if _group_topic_allows(group, "main"):
        return "main"
    return ""


def _topic_lock_allows(chat_id: str, thread_id: str) -> bool:
    lock, group = _topic_lock_group(chat_id)
    if not _topic_lock_enabled(lock) or not group:
        return True
    return _group_topic_allows(group, thread_id)


def _message_topic_allowed(message: Any) -> bool:
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", None) == "private":
        return True
    chat_id = _message_chat_id(message)
    thread_id = _topic_id(getattr(message, "message_thread_id", None))
    allowed = _topic_lock_allows(chat_id, thread_id)
    if not allowed:
        print(
            "[morneven-topic-lock] "
            f"topic_lock_drop chat={chat_id or '-'} thread={thread_id} "
            f"runtime={os.environ.get('MORNEVEN_RUNTIME_ID', '-') or '-'}",
            flush=True,
        )
    return allowed


def _metadata_thread_id(metadata: dict[str, Any]) -> str:
    return _topic_id(
        metadata.get(
            "message_thread_id",
            metadata.get("thread_id", metadata.get("topic_id")),
        )
    )


def _has_explicit_metadata_thread(metadata: dict[str, Any]) -> bool:
    for key in ("message_thread_id", "thread_id", "topic_id"):
        if key not in metadata:
            continue
        value = metadata.get(key)
        if _topic_id(value) != "main":
            return True
    return False


def _coerced_metadata_thread_value(thread_id: str) -> int | str:
    try:
        return _coerce_message_thread_id(thread_id) if thread_id != "main" else "main"
    except Exception:
        return thread_id


def _set_outbound_message_thread(msg: Any, thread_id: str) -> None:
    metadata = getattr(msg, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
    if thread_id == "main":
        metadata.pop("message_thread_id", None)
        metadata.pop("thread_id", None)
        metadata.pop("topic_id", None)
    else:
        metadata["message_thread_id"] = _coerced_metadata_thread_value(thread_id)
    try:
        setattr(msg, "metadata", metadata)
    except Exception:
        pass


def _prepare_outbound_topic(msg: Any) -> bool:
    chat_id = str(getattr(msg, "chat_id", "") or "")
    if not chat_id.startswith("-"):
        return True
    metadata = getattr(msg, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    thread_id = _metadata_thread_id(metadata)
    lock, group = _topic_lock_group(chat_id)
    if not _topic_lock_enabled(lock) or not group:
        return True
    if _group_topic_allows(group, thread_id):
        return True
    primary_topic = _group_primary_topic(group)
    if primary_topic and not _has_explicit_metadata_thread(metadata):
        _set_outbound_message_thread(msg, primary_topic)
        print(
            "[morneven-topic-lock] "
            f"topic_lock_redirect chat={chat_id or '-'} thread={thread_id} "
            f"target={primary_topic} runtime={os.environ.get('MORNEVEN_RUNTIME_ID', '-') or '-'}",
            flush=True,
        )
        return True
    print(
        "[morneven-topic-lock] "
        f"topic_lock_block chat={chat_id or '-'} thread={thread_id} "
        f"runtime={os.environ.get('MORNEVEN_RUNTIME_ID', '-') or '-'}",
        flush=True,
    )
    return False


def _prepare_outbound_metadata(chat_id: str, metadata: dict[str, Any] | None) -> tuple[bool, dict[str, Any] | None]:
    probe_metadata = metadata if isinstance(metadata, dict) else {}
    probe = type("_MornevenOutboundProbe", (), {"chat_id": chat_id, "metadata": dict(probe_metadata)})()
    allowed = _prepare_outbound_topic(probe)
    if not allowed:
        return False, metadata
    next_metadata = getattr(probe, "metadata", probe_metadata)
    if isinstance(next_metadata, dict) and next_metadata != probe_metadata:
        return True, next_metadata
    return True, metadata


def _message_usernames(pattern: re.Pattern[str], text: str) -> set[str]:
    return {_normalize_username(match) for match in pattern.findall(text) if _normalize_username(match)}


def _message_entity_usernames(message: Any, text: str) -> set[str]:
    usernames: set[str] = set()
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
    for entity in entities:
        entity_type = str(getattr(entity, "type", "") or "")
        if entity_type == "text_mention":
            user = getattr(entity, "user", None)
            username = _normalize_username(getattr(user, "username", None))
            if username:
                usernames.add(username)
            continue
        if entity_type != "mention":
            continue
        mention = ""
        extract_from = getattr(entity, "extract_from", None)
        if callable(extract_from):
            try:
                mention = str(extract_from(text) or "")
            except Exception:
                mention = ""
        if not mention:
            try:
                offset = int(getattr(entity, "offset", 0) or 0)
                length = int(getattr(entity, "length", 0) or 0)
                mention = text[offset:offset + length]
            except Exception:
                mention = ""
        username = _normalize_username(mention)
        if username:
            usernames.add(username)
    return usernames


def _message_target_usernames(text: str, message: Any | None = None) -> set[str]:
    targets = _message_usernames(COMMAND_TARGETS_RE, text) | _message_usernames(MENTIONS_RE, text)
    if message is not None:
        targets |= _message_entity_usernames(message, text)
    return targets


def _message_mentions_bot(username: str, text: str, message: Any | None = None) -> bool:
    if not username:
        return False
    command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
    if username in command_targets:
        return True
    return username in _message_target_usernames(text, message)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _coerce_message_thread_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("message_thread_id must be a numeric Telegram topic ID")
    text = str(value).strip()
    if not text:
        return None
    if not re.fullmatch(r"-?\d+", text):
        raise ValueError("message_thread_id must be a numeric Telegram topic ID")
    return int(text)


async def _bot_username(channel: Any) -> str:
    resolved = _normalize_username(getattr(channel, "_morneven_resolved_bot_username", None))
    if resolved:
        return resolved

    cached = _normalize_username(getattr(channel, "_bot_username", None))
    if cached:
        return cached

    env_username = _env_username("MORNEVEN_TELEGRAM_BOT_USERNAME")
    if env_username:
        return env_username

    ensure_identity = getattr(channel, "_ensure_bot_identity", None)
    if callable(ensure_identity):
        try:
            _, username = await _maybe_await(ensure_identity())
            username = _normalize_username(username)
            if username:
                setattr(channel, "_morneven_resolved_bot_username", username)
                return username
        except Exception:
            return ""

    return ""


async def _username_from_bot(bot: Any) -> str:
    if not bot:
        return ""

    username = _normalize_username(getattr(bot, "username", None))
    if username:
        return username

    get_me = getattr(bot, "get_me", None)
    if not callable(get_me):
        return ""
    try:
        me = await _maybe_await(get_me())
    except Exception:
        return ""
    if isinstance(me, dict):
        return _normalize_username(me.get("username"))
    return _normalize_username(getattr(me, "username", None))


async def _cache_context_bot_username(channel: Any, context: Any) -> None:
    candidates = []
    if context is not None:
        candidates.append(getattr(context, "bot", None))
        application = getattr(context, "application", None)
        candidates.append(getattr(application, "bot", None))
    for candidate in candidates:
        username = await _username_from_bot(candidate)
        if username:
            setattr(channel, "_morneven_resolved_bot_username", username)
            return


def _registered_bot_mentions(text: str, message: Any | None = None) -> set[str]:
    registered = _env_usernames("MORNEVEN_TELEGRAM_ACTIVE_BOTS")
    if not registered:
        return set()
    return _message_target_usernames(text, message) & registered


async def _targeted_at_other_bot(channel: Any, message: Any) -> bool:
    text = _message_text(message)
    if not text:
        return False

    username = await _bot_username(channel)
    if not username:
        return False

    targets = _message_target_usernames(text, message)
    if username in targets or _message_mentions_bot(username, text, message):
        _debug_routing(message, username, targets, "allow", "targeted_current_bot")
        return False

    command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
    registered_mentions = targets & _env_usernames("MORNEVEN_TELEGRAM_ACTIVE_BOTS")
    if registered_mentions:
        _debug_routing(message, username, targets, "drop", "targeted_other_registered_bot")
        return True

    if command_targets:
        _debug_routing(message, username, targets, "drop", "targeted_other_command")
        return True

    command_match = COMMAND_TARGET_RE.match(text)
    if command_match:
        is_other = _normalize_username(command_match.group(1)) != username
        if is_other:
            _debug_routing(message, username, targets, "drop", "leading_other_command")
        return is_other

    mention_match = LEADING_MENTION_RE.match(text)
    if mention_match:
        is_other = _normalize_username(mention_match.group(1)) != username
        if is_other:
            _debug_routing(message, username, targets, "drop", "leading_other_mention")
        return is_other

    return False


async def _command_allowed_for_group(channel: Any, message: Any) -> bool:
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", None) == "private":
        return True

    checker = getattr(channel, "_is_group_message_for_bot", None)
    if callable(checker):
        try:
            return bool(await _maybe_await(checker(message)))
        except Exception:
            return False

    return True


def _patch_message_tool_thread_id() -> None:
    try:
        from nanobot.agent.tools.base import tool_parameters
        from nanobot.agent.tools.message import MessageTool
        from nanobot.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema, tool_parameters_schema
        from nanobot.bus.events import OutboundMessage
    except Exception:
        return

    if getattr(MessageTool, "_morneven_thread_message_patch", False):
        return

    tool_parameters(
        tool_parameters_schema(
            content=StringSchema(
                "Message content for proactive or cross-channel delivery. "
                "Do not use this for a normal reply in the current chat."
            ),
            channel=StringSchema(
                "Optional target channel for cross-channel/proactive delivery. "
                "Do not set this to the current runtime channel for a normal reply."
            ),
            chat_id=StringSchema(
                "Optional target chat/user ID for cross-channel/proactive delivery. "
                "On WebSocket/WebUI turns: omit chat_id to use the server conversation id. "
                "Do not set this to the current runtime chat for a normal reply."
            ),
            message_thread_id=StringSchema(
                "Optional Telegram forum topic ID. Use with channel='telegram' and a group "
                "chat_id to send into that topic instead of the main group."
            ),
            metadata=ObjectSchema(
                {
                    "message_thread_id": StringSchema("Telegram forum topic ID."),
                    "thread_id": StringSchema("Alias for message_thread_id."),
                    "topic_id": StringSchema("Alias for message_thread_id."),
                },
                description=(
                    "Optional channel metadata. For Telegram topics, prefer the top-level "
                    "message_thread_id parameter. metadata.message_thread_id is accepted for "
                    "compatibility."
                ),
            ),
            media=ArraySchema(
                StringSchema(""),
                description=(
                    "Optional list of existing file paths to attach for proactive or "
                    "cross-channel delivery."
                ),
            ),
            buttons=ArraySchema(
                ArraySchema(StringSchema("Button label")),
                description="Optional inline keyboard buttons as list of rows.",
            ),
            required=["content"],
        )
    )(MessageTool)

    async def _execute(
        self: Any,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        message_thread_id: str | int | None = None,
        media: list[str] | None = None,
        buttons: list[list[str]] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.utils.helpers import strip_think

        content = strip_think(content)

        if buttons is not None:
            if not isinstance(buttons, list) or any(
                not isinstance(row, list) or any(not isinstance(label, str) for label in row)
                for row in buttons
            ):
                return "Error: buttons must be a list of list of strings"

        incoming_metadata = metadata if metadata is not None else kwargs.get("metadata")
        if incoming_metadata is not None and not isinstance(incoming_metadata, dict):
            return "Error: metadata must be an object"
        incoming_metadata = incoming_metadata or {}

        explicit_thread_id = (
            message_thread_id
            if message_thread_id is not None
            else kwargs.get(
                "message_thread_id",
                kwargs.get(
                    "thread_id",
                    kwargs.get(
                        "topic_id",
                        incoming_metadata.get(
                            "message_thread_id",
                            incoming_metadata.get("thread_id", incoming_metadata.get("topic_id")),
                        ),
                    ),
                ),
            )
        )
        try:
            telegram_thread_id = _coerce_message_thread_id(explicit_thread_id)
        except ValueError as exc:
            return f"Error: {str(exc)}"

        default_channel = self._default_channel.get()
        default_chat_id = self._default_chat_id.get()
        channel = channel or default_channel
        explicit_chat_id = chat_id
        if (
            default_channel == "websocket"
            and channel == "websocket"
            and explicit_chat_id is not None
            and str(explicit_chat_id).strip() != ""
            and str(explicit_chat_id).strip() != str(default_chat_id).strip()
        ):
            return (
                "Error: chat_id does not match the active WebSocket conversation. "
                "Omit chat_id and usually channel so delivery uses the current conversation id."
            )
        chat_id = chat_id or default_chat_id
        same_target = channel == default_channel and chat_id == default_chat_id
        if same_target:
            message_id = message_id or self._default_message_id.get()
        else:
            message_id = None

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        if media:
            try:
                media = self._resolve_media(media)
            except (OSError, PermissionError, ValueError) as exc:
                return f"Error: media path is not allowed: {str(exc)}"

        outbound_metadata = dict(self._default_metadata.get()) if same_target else {}
        if message_id:
            outbound_metadata["message_id"] = message_id
        if telegram_thread_id is not None:
            outbound_metadata["message_thread_id"] = telegram_thread_id
        if self._record_channel_delivery_var.get() or media:
            outbound_metadata["_record_channel_delivery"] = True

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            buttons=buttons or [],
            metadata=outbound_metadata,
        )

        try:
            await self._send_callback(msg)
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn = True
                if media:
                    prev = self._turn_delivered_media_var.get()
                    self._turn_delivered_media_var.set(prev + tuple(str(path) for path in media))
            media_info = f" with {len(media)} attachments" if media else ""
            button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
            thread_info = f" topic:{telegram_thread_id}" if telegram_thread_id is not None else ""
            return f"Message sent to {channel}:{chat_id}{thread_info}{media_info}{button_info}"
        except Exception as exc:
            return f"Error sending message: {str(exc)}"

    _execute._morneven_thread_message_patch = True  # type: ignore[attr-defined]
    MessageTool.execute = _execute
    MessageTool._morneven_thread_message_patch = True


def _patch_telegram_channel() -> None:
    try:
        from nanobot.channels.telegram import TelegramChannel
    except Exception:
        return

    original_group_checker = getattr(TelegramChannel, "_is_group_message_for_bot", None)
    if original_group_checker and not getattr(original_group_checker, "_morneven_multi_mention_filter", False):

        async def _is_group_message_for_bot(self: Any, message: Any) -> bool:
            text = _message_text(message)
            username = await _bot_username(self)
            if username and _message_mentions_bot(username, text, message):
                return True
            command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
            mentions = _message_target_usernames(text, message)
            registered_mentions = _registered_bot_mentions(text, message)
            if username and registered_mentions:
                return False
            if username and (command_targets or mentions):
                return False
            return bool(await _maybe_await(original_group_checker(self, message)))

        _is_group_message_for_bot._morneven_multi_mention_filter = True  # type: ignore[attr-defined]
        TelegramChannel._is_group_message_for_bot = _is_group_message_for_bot

    original_forward_command = getattr(TelegramChannel, "_forward_command", None)
    if original_forward_command and not getattr(original_forward_command, "_morneven_target_filter", False):

        async def _forward_command(self: Any, update: Any, context: Any) -> None:
            message = getattr(update, "message", None)
            await _cache_context_bot_username(self, context)
            if message is not None:
                _record_topic(message)
                if not _message_topic_allowed(message):
                    return
                if await _targeted_at_other_bot(self, message):
                    return
                if not await _command_allowed_for_group(self, message):
                    return
            await original_forward_command(self, update, context)

        _forward_command._morneven_target_filter = True  # type: ignore[attr-defined]
        TelegramChannel._forward_command = _forward_command

    for method_name in ("_on_start", "_on_help"):
        original = getattr(TelegramChannel, method_name, None)
        if not original or getattr(original, "_morneven_target_filter", False):
            continue

        async def _command_handler(self: Any, update: Any, context: Any, _original: Any = original) -> None:
            message = getattr(update, "message", None)
            await _cache_context_bot_username(self, context)
            if message is not None:
                _record_topic(message)
                if not _message_topic_allowed(message):
                    return
                if await _targeted_at_other_bot(self, message):
                    return
                if not await _command_allowed_for_group(self, message):
                    return
            await _original(self, update, context)

        _command_handler._morneven_target_filter = True  # type: ignore[attr-defined]
        setattr(TelegramChannel, method_name, _command_handler)

    original_on_message = getattr(TelegramChannel, "_on_message", None)
    if original_on_message and not getattr(original_on_message, "_morneven_target_filter", False):

        async def _on_message(self: Any, update: Any, context: Any) -> None:
            message = getattr(update, "message", None)
            await _cache_context_bot_username(self, context)
            if message is not None:
                _record_topic(message)
                if not _message_topic_allowed(message):
                    return
                username = await _bot_username(self)
                targets = _message_target_usernames(_message_text(message), message)
                _debug_ingress(message, username, targets, "on_message")
                if await _targeted_at_other_bot(self, message):
                    return
            await original_on_message(self, update, context)

        _on_message._morneven_target_filter = True  # type: ignore[attr-defined]
        TelegramChannel._on_message = _on_message

    original_send = getattr(TelegramChannel, "send", None)
    if original_send and not getattr(original_send, "_morneven_topic_lock_filter", False):

        async def send(self: Any, msg: Any) -> None:
            if not _prepare_outbound_topic(msg):
                raise RuntimeError("Telegram topic blocked by Topic Lock")
            await original_send(self, msg)

        send._morneven_topic_lock_filter = True  # type: ignore[attr-defined]
        TelegramChannel.send = send

    original_send_delta = getattr(TelegramChannel, "send_delta", None)
    if original_send_delta and not getattr(original_send_delta, "_morneven_topic_lock_filter", False):

        async def send_delta(self: Any, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
            allowed, next_metadata = _prepare_outbound_metadata(chat_id, metadata)
            if not allowed:
                raise RuntimeError("Telegram topic blocked by Topic Lock")
            await original_send_delta(self, chat_id, delta, next_metadata)

        send_delta._morneven_topic_lock_filter = True  # type: ignore[attr-defined]
        TelegramChannel.send_delta = send_delta


def _auto_dream_enabled() -> bool:
    value = os.environ.get("MORNEVEN_AUTO_DREAM_ENABLED", "").strip().lower()
    return value not in {"0", "false", "off", "no"}


def _patch_cron_auto_dream() -> None:
    try:
        from nanobot.cron.service import CronService
    except Exception:
        return

    original_register = getattr(CronService, "register_system_job", None)
    if not original_register or getattr(original_register, "_morneven_auto_dream_patch", False):
        return

    def register_system_job(self: Any, job: Any) -> Any:
        if getattr(job, "name", "") == "dream" and not _auto_dream_enabled():
            store = self._load_store()
            if store is not None:
                store.jobs = [
                    item
                    for item in store.jobs
                    if getattr(item, "id", "") != getattr(job, "id", "") and getattr(item, "name", "") != "dream"
                ]
                self._save_store()
            return job
        return original_register(self, job)

    register_system_job._morneven_auto_dream_patch = True  # type: ignore[attr-defined]
    CronService.register_system_job = register_system_job


_patch_message_tool_thread_id()
_patch_telegram_channel()
_patch_cron_auto_dream()
