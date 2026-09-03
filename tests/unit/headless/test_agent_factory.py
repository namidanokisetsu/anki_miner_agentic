import json
from dataclasses import replace

import pytest

from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig
from anki_miner.agent.policy import SETTING_OWNERSHIP
from anki_miner.config import AnkiMinerConfig, ChainEntry, FreqEntry
from anki_miner.gui.utils.config_manager import GUIConfigManager
from anki_miner.runtime.agent_factory import _mining_config, load_agent_config


def test_agent_cannot_shadow_runtime_paths_or_mining_policy(tmp_path):
    for key, value in (("ffmpeg_location", str(tmp_path / "ffmpeg")), ("audio_padding", 0.25)):
        with pytest.raises(AgentMiningError) as raised:
            _mining_config({key: value})
        assert raised.value.code == "unsupported_agent_config_key"
        assert raised.value.details["key"] == key


def test_agent_config_rejects_string_boolean_and_unknown_fields():
    base = {
        "knowledge_sources": [{"deck": "Known", "note_type": "ExampleNote", "word_fields": ["word"]}],
        "write_target": {"deck": "Mining", "note_type": "ExampleNote", "enabled": "false"},
    }
    with pytest.raises(AgentMiningError, match="enabled must be boolean"):
        AgentProfileConfig.from_dict(base)

    base["write_target"]["enabled"] = False
    base["max_card"] = 10
    with pytest.raises(AgentMiningError, match="Unknown agent configuration fields"):
        AgentProfileConfig.from_dict(base)


def test_effective_setting_ownership_is_closed_and_single_source():
    assert {entry.owner for entry in SETTING_OWNERSHIP.values()} == {
        "gui_mining_policy",
        "agent_config",
        "run_authorization",
    }
    assert SETTING_OWNERSHIP["mining_policy"].source == "active_gui_profile"
    assert SETTING_OWNERSHIP["max_cards"].source == "prepare_mining_run.max_cards"


def test_agent_inherits_gui_mining_settings_without_runtime_shadows(tmp_path, monkeypatch):
    gui_config = replace(
        AnkiMinerConfig(),
        audio_padding=0.75,
        dictionary_chain=(
            ChainEntry("indexed", "gui-jmdict"),
            ChainEntry("indexed", "gui-kokugo"),
        ),
        frequency_chain=(FreqEntry("gui-frequency"),),
        subtitle_offset=-2.5,
    )
    monkeypatch.setattr(GUIConfigManager, "load_config", classmethod(lambda cls: gui_config))
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "knowledge_sources": [
                        {
                            "deck": "Known",
                            "note_type": "ExampleNote",
                            "word_fields": ["word"],
                            "text_fields": ["sentence"],
                        }
                    ],
                    "write_target": {"deck": "Mining", "note_type": "ExampleNote"},
                    "chosen_definition_field": "Chosen",
                    "sentence_translation_field": "Translation",
                }
            }
        ),
        encoding="utf-8",
    )

    _storage, _profile, mining = load_agent_config(path)

    assert mining.audio_padding == 0.75
    assert mining.ffmpeg_location == gui_config.ffmpeg_location
    assert mining.dictionary_chain == (
        ChainEntry("indexed", "gui-jmdict"),
        ChainEntry("indexed", "gui-kokugo"),
    )
    assert mining.frequency_chain == (FreqEntry("gui-frequency"),)
    assert mining.subtitle_offset == -2.5
    assert mining.anki_fields["chosen_definition"] == "Chosen"
    assert mining.anki_fields["sentence_translation"] == "Translation"


def test_agent_rejects_mining_policy_overrides(tmp_path, monkeypatch):
    gui_config = replace(
        AnkiMinerConfig(),
        dictionary_chain=(ChainEntry("indexed", "gui-dictionary"),),
        subtitle_offset=-2.5,
    )
    monkeypatch.setattr(GUIConfigManager, "load_config", classmethod(lambda cls: gui_config))
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "knowledge_sources": [
                        {
                            "deck": "Known",
                            "note_type": "ExampleNote",
                            "word_fields": ["word"],
                            "text_fields": ["sentence"],
                        }
                    ],
                    "write_target": {"deck": "Mining", "note_type": "ExampleNote"},
                },
                "mining": {
                    "dictionary_chain": [{"kind": "indexed", "dict_id": "agent-dictionary", "enabled": True}],
                    "subtitle_offset": 1.25,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentMiningError) as raised:
        load_agent_config(path)
    assert raised.value.code == "unsupported_agent_config_key"
    assert raised.value.details["key"] == "mining.dictionary_chain"


def test_legacy_policy_keys_are_rejected_even_when_false(tmp_path, monkeypatch):
    monkeypatch.setattr(GUIConfigManager, "load_config", classmethod(lambda cls: AnkiMinerConfig()))
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "knowledge_sources": [{"deck": "Known", "note_type": "ExampleNote", "word_fields": ["word"]}],
                    "write_target": {"deck": "Mining", "note_type": "ExampleNote"},
                    "exclude_katakana_only": False,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentMiningError) as raised:
        load_agent_config(path)
    assert raised.value.code == "unsupported_agent_config_key"
    assert raised.value.details == {
        "key": "exclude_katakana_only",
        "owner": "Settings → Filtering",
        "action": "Remove 'exclude_katakana_only' from the configured agent JSON file",
    }


@pytest.mark.parametrize(
    ("key", "value", "owner"),
    [
        ("max_cards", 300, "the prepare_mining_run request"),
        ("max_payload_bytes", 512_000, "Anki Miner internal transport"),
        ("review_pool_size", 20, "the prepare_mining_run request"),
    ],
)
def test_agent_config_rejects_removed_limit_keys(tmp_path, monkeypatch, key, value, owner):
    monkeypatch.setattr(GUIConfigManager, "load_config", classmethod(lambda cls: AnkiMinerConfig()))
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "knowledge_sources": [{"deck": "Known", "note_type": "ExampleNote", "word_fields": ["word"]}],
                    "write_target": {"deck": "Mining", "note_type": "ExampleNote"},
                    key: value,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentMiningError) as raised:
        load_agent_config(path)

    assert raised.value.code == "unsupported_agent_config_key"
    assert raised.value.details["key"] == key
    assert raised.value.details["owner"] == owner
