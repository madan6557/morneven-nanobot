"""Runtime compatibility patches for Morneven Nanobot."""

from __future__ import annotations

import inspect
import os
import re
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


def _message_usernames(pattern: re.Pattern[str], text: str) -> set[str]:
    return {_normalize_username(match) for match in pattern.findall(text) if _normalize_username(match)}


def _message_mentions_bot(username: str, text: str) -> bool:
    if not username:
        return False
    command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
    if username in command_targets:
        return True
    mentions = _message_usernames(MENTIONS_RE, text)
    return username in mentions


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
    env_username = _env_username("MORNEVEN_TELEGRAM_BOT_USERNAME")
    if env_username:
        return env_username

    cached = _normalize_username(getattr(channel, "_bot_username", None))
    if cached:
        return cached

    ensure_identity = getattr(channel, "_ensure_bot_identity", None)
    if callable(ensure_identity):
        try:
            _, username = await _maybe_await(ensure_identity())
            username = _normalize_username(username)
            if username:
                return username
        except Exception:
            return ""

    return ""


def _registered_bot_mentions(text: str) -> set[str]:
    registered = _env_usernames("MORNEVEN_TELEGRAM_ACTIVE_BOTS")
    if not registered:
        return set()
    message_targets = _message_usernames(COMMAND_TARGETS_RE, text) | _message_usernames(MENTIONS_RE, text)
    return message_targets & registered


async def _targeted_at_other_bot(channel: Any, message: Any) -> bool:
    text = _message_text(message)
    if not text:
        return False

    username = await _bot_username(channel)
    if not username:
        return False

    if _message_mentions_bot(username, text):
        return False

    command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
    registered_mentions = _registered_bot_mentions(text)
    if registered_mentions:
        return True

    if command_targets:
        return True

    command_match = COMMAND_TARGET_RE.match(text)
    if command_match:
        return _normalize_username(command_match.group(1)) != username

    mention_match = LEADING_MENTION_RE.match(text)
    if mention_match:
        return _normalize_username(mention_match.group(1)) != username

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
            if username and _message_mentions_bot(username, text):
                return True
            command_targets = _message_usernames(COMMAND_TARGETS_RE, text)
            mentions = _message_usernames(MENTIONS_RE, text)
            registered_mentions = _registered_bot_mentions(text)
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
            if message is not None:
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
            if message is not None:
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
            if message is not None and await _targeted_at_other_bot(self, message):
                return
            await original_on_message(self, update, context)

        _on_message._morneven_target_filter = True  # type: ignore[attr-defined]
        TelegramChannel._on_message = _on_message


_patch_message_tool_thread_id()
_patch_telegram_channel()
