"""Named ability packs for experiment conditions.

``baseline``
    Non-seers cannot claim seer. Wolves cannot empty-knife or self-knife.

``cheap_talk``
    All catalog abilities are legal: anyone may seer-claim; wolves may
    empty-knife and self-knife.
"""

from __future__ import annotations

import os
from typing import Any

BASELINE = "baseline"
CHEAP_TALK = "cheap_talk"

UNSET: Any = object()

_ALIASES = {
    "baseline": BASELINE,
    "cheap_talk": CHEAP_TALK,
    "cheap-talk": CHEAP_TALK,
    "cheaptalk": CHEAP_TALK,
    "cheap talk": CHEAP_TALK,
}

PRESETS: dict[str, dict[str, bool]] = {
    BASELINE: {
        "honesty_rule": True,
        "wolf_self_knife": False,
        "wolf_empty_knife": False,
        "wolf_night_chat": True,
    },
    CHEAP_TALK: {
        "honesty_rule": False,
        "wolf_self_knife": True,
        "wolf_empty_knife": True,
        "wolf_night_chat": True,
    },
}

# Legacy WerewolfGame defaults when no preset is named.
_NO_PRESET_DEFAULTS: dict[str, bool] = {
    "honesty_rule": False,
    "wolf_self_knife": False,
    "wolf_empty_knife": True,
    "wolf_night_chat": True,
}

def normalize_preset(name: str | None) -> str | None:
    """Map UI / API aliases onto a canonical preset id, or None if unset."""
    if name is None:
        return None
    text = str(name).strip().lower()
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    raise ValueError(f"unknown rule_preset={name!r}; expected {BASELINE!r} or {CHEAP_TALK!r}")


def preset_from_rule_pack(rule_pack_id: str | None) -> str | None:
    """Interpret create-game rule_pack_id as a preset when it matches one."""
    if rule_pack_id is None:
        return None
    text = str(rule_pack_id).strip().lower()
    if not text or text == "wolfcha-default":
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    return None


def resolve_ability_flags(
    rule_preset: str | None = None,
    *,
    honesty_rule: Any = UNSET,
    wolf_self_knife: Any = UNSET,
    wolf_empty_knife: Any = UNSET,
    wolf_night_chat: Any = UNSET,
) -> dict[str, Any]:
    """Merge a named preset with explicit per-flag overrides."""
    preset_id = normalize_preset(rule_preset) if rule_preset else None
    flags: dict[str, Any] = dict(PRESETS[preset_id] if preset_id else _NO_PRESET_DEFAULTS)
    flags["rule_preset"] = preset_id
    overrides = {
        "honesty_rule": honesty_rule,
        "wolf_self_knife": wolf_self_knife,
        "wolf_empty_knife": wolf_empty_knife,
        "wolf_night_chat": wolf_night_chat,
    }
    for key, value in overrides.items():
        if value is not UNSET:
            flags[key] = bool(value)
    return flags


def wolf_night_option_tokens(flags: dict[str, Any]) -> set[str]:
    opts: set[str] = set()
    if flags.get("wolf_self_knife"):
        opts.add("self")
    if flags.get("wolf_empty_knife"):
        opts.add("empty")
    return opts


def env_rule_preset() -> str | None:
    """Read AIWEREWOLF_RULE_PRESET from the process environment."""
    raw = os.getenv("AIWEREWOLF_RULE_PRESET", "").strip()
    return normalize_preset(raw) if raw else None


def env_optional_bool(name: str) -> Any:
    """Return UNSET when the env var is missing/blank, otherwise a bool."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return UNSET
    return raw.lower() in {"1", "true", "yes", "on"}


def env_ability_overrides() -> dict[str, Any]:
    """Per-flag .env overrides; unset keys stay UNSET so the preset wins."""
    return {
        "honesty_rule": env_optional_bool("AIWEREWOLF_HONESTY_RULE"),
        "wolf_self_knife": env_optional_bool("AIWEREWOLF_WOLF_SELF_KNIFE"),
        "wolf_empty_knife": env_optional_bool("AIWEREWOLF_WOLF_EMPTY_KNIFE"),
        "wolf_night_chat": env_optional_bool("AIWEREWOLF_WOLF_NIGHT_CHAT"),
    }


def resolve_web_rule_preset(rule_preset: str | None = None, rule_pack_id: str | None = None) -> str | None:
    """API query > named rule_pack_id > .env AIWEREWOLF_RULE_PRESET."""
    if rule_preset:
        return normalize_preset(rule_preset)
    from_pack = preset_from_rule_pack(rule_pack_id)
    if from_pack:
        return from_pack
    return env_rule_preset()
