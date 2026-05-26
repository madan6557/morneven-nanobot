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


_patch_telegram_channel()
