"""Controlled procedural memory plus user-authored instructions; no automatic promotion.

Also the rules a user's edit to that memory must pass, so every writer applies the same ones.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contextforge.core.context.prompts import load_prompt
from contextforge.core.entities.user import User
from contextforge.core.services.credentials import contains_credential

DEFAULT_TIMEZONE = "Asia/Kolkata"
_PREFERENCE_FORMATS = {
    "currency": (re.compile(r"[A-Z]{3}"), "Currency must be an ISO 4217 code such as INR."),
    "locale": (
        # BCP 47 language, optional script and region: en, en-IN, zh-Hans-CN, es-419.
        re.compile(r"[a-zA-Z]{2,3}(?:-[a-zA-Z]{4})?(?:-(?:[a-zA-Z]{2}|[0-9]{3}))?"),
        "Locale must be a BCP 47 tag such as en-IN.",
    ),
}


class MemorySnapshot(TypedDict):
    text: str
    version: str
    preferences: dict[str, str]


class MemoryEditRefused(ValueError):
    """An instructions or preferences edit the memory tier refuses; ``code`` is the API code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def memory_snapshot(user: User | None) -> MemorySnapshot:
    text = load_prompt("agent-memory")
    if user and user.instructions:
        text += (
            "\n\nUser standing instructions (subject to application permissions):\n"
            + user.instructions
        )
    preferences = dict(user.preferences or {}) if user else {}
    version = hashlib.sha256((text + json.dumps(preferences, sort_keys=True)).encode()).hexdigest()[
        :16
    ]
    return MemorySnapshot(text=text, version=version, preferences=preferences)


def user_timezone(memory: MemorySnapshot) -> str:
    """The timezone the user's dates are read in: their preference, else the default."""
    return memory["preferences"].get("timezone", DEFAULT_TIMEZONE)


def merged_preferences(
    current: Mapping[str, str] | None, patch: Mapping[str, str]
) -> dict[str, str]:
    """``patch`` merged key by key over ``current``; raises ``MemoryEditRefused`` for an unknown
    key or a value in the wrong format."""
    for key, value in patch.items():
        _check_preference(key, value)
    return {**(current or {}), **patch}


def checked_instructions(text: str | None) -> str | None:
    """``text`` stripped, None when blank; raises ``MemoryEditRefused`` when it holds a credential,
    because instructions are sent with every model call."""
    stripped = (text or "").strip()
    if contains_credential(stripped):
        raise MemoryEditRefused(
            "credential_in_instructions",
            "Instructions cannot contain passwords, keys or tokens.",
        )
    return stripped or None


def _check_preference(key: str, value: str) -> None:
    if key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MemoryEditRefused("validation", "Unknown timezone.") from exc
        return
    if key not in _PREFERENCE_FORMATS:
        raise MemoryEditRefused("validation", f"Unknown preference {key}.")
    pattern, message = _PREFERENCE_FORMATS[key]
    if not pattern.fullmatch(value):
        raise MemoryEditRefused("validation", message)
