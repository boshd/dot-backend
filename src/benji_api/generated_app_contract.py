from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DOT_REMINDER_CREATE_CAPABILITY = "dot.reminder.create"
SUPPORTED_GENERATED_APP_CAPABILITIES = frozenset({DOT_REMINDER_CREATE_CAPABILITY})


class GeneratedAppCapabilityError(ValueError):
    pass


def parse_generated_app_capabilities(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the small, explicit authority grant carried by an app revision."""

    raw = manifest.get("capabilities", [])
    if not isinstance(raw, list) or len(raw) > len(SUPPORTED_GENERATED_APP_CAPABILITIES):
        raise GeneratedAppCapabilityError("Manifest capabilities must be a supported list")
    if any(not isinstance(item, str) for item in raw):
        raise GeneratedAppCapabilityError("Manifest capabilities must be strings")
    if len(set(raw)) != len(raw):
        raise GeneratedAppCapabilityError("Manifest capabilities must be unique")
    unsupported = set(raw) - SUPPORTED_GENERATED_APP_CAPABILITIES
    if unsupported:
        raise GeneratedAppCapabilityError("Manifest requests an unsupported capability")
    return tuple(raw)
