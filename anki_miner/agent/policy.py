"""Single-source ownership and bounded policy evidence for agent mining."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from anki_miner.gui.utils.config_manager import GUIConfigManager

from .models import canonical_json

SettingOwner = Literal["gui_mining_policy", "agent_config", "run_authorization"]


@dataclass(frozen=True)
class SettingOwnership:
    owner: SettingOwner
    source: str


# This is deliberately executable data rather than documentation. Tests assert
# uniqueness and config loading consults the same table when rejecting shadows.
SETTING_OWNERSHIP: dict[str, SettingOwnership] = {
    "mining_policy": SettingOwnership("gui_mining_policy", "active_gui_profile"),
    "knowledge_sources": SettingOwnership("agent_config", "agent.knowledge_sources"),
    "write_target": SettingOwnership("agent_config", "agent.write_target"),
    "enrichment_fields": SettingOwnership("agent_config", "agent.*_field"),
    "max_cards": SettingOwnership("run_authorization", "prepare_mining_run.max_cards"),
}

MAX_SENTENCE_VARIANTS = 4
MAX_RATIONALE_CHARS = 500
MAX_DEFINITION_OPTIONS = 12
MAX_DEFINITION_OPTION_CHARS = 2_000
MAX_CHOSEN_DEFINITION_CHARS = 240
MAX_SENTENCE_TRANSLATION_CHARS = 500

GUI_MINING_POLICY_FIELDS = (
    "allowed_pos",
    "excluded_subtypes",
    "excluded_wordsets",
    "dictionary_chain",
    "frequency_chain",
    "min_frequency_rank",
    "max_frequency_rank",
    "frequency_keep_unranked",
    "include_known_words",
    "use_known_words_db",
    "known_words_match_kana_variants",
    "blacklist_path",
    "whitelist_path",
    "use_blacklist",
    "use_whitelist",
    "exclude_hiragana_only_words",
    "exclude_katakana_only_words",
    "deduplicate_sentences",
    "use_i_plus_one_filter",
    "use_sentence_length_filter",
    "max_sentence_duration_seconds",
    "max_sentence_chars",
    "subtitle_offset",
    "subtitle_regex_filter",
    "subtitle_regex_replacement",
    "use_subtitle_regex_filter",
    "pitch_category_format",
)


def serialized_gui_policy(config: Any) -> dict[str, Any]:
    """Return the effective GUI-owned policy without paths or agent mappings."""
    value = GUIConfigManager._paths_to_strings(GUIConfigManager._config_to_serializable_dict(config))
    return {key: value[key] for key in GUI_MINING_POLICY_FIELDS}


def _bounded_value(value: Any) -> Any:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) <= 1_024:
        return value
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "preview": encoded[:256].decode("utf-8", errors="replace"),
    }


def effective_policy_inspection(config: Any, fingerprint: str) -> dict[str, Any]:
    values = serialized_gui_policy(config)
    from anki_miner.config import AnkiMinerConfig

    defaults = serialized_gui_policy(AnkiMinerConfig())
    return {
        "fingerprint": fingerprint,
        "source": "active_gui_profile",
        "settings": [
            {
                "setting": key,
                "value": _bounded_value(values[key]),
                "source": "active_gui_profile",
                "explicit": values[key] != defaults[key],
            }
            for key in sorted(values)
        ],
        "conflicts": [],
        "canonical_bytes": len(canonical_json(values).encode("utf-8")),
    }
