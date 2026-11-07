"""Controlled procedural memory plus user-authored instructions; no automatic promotion."""

import hashlib
import json
from typing import TypedDict

from contextforge.core.context.prompts import load_prompt
from contextforge.core.entities.user import User


class MemorySnapshot(TypedDict):
    text: str
    version: str
    preferences: dict[str, str]


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
