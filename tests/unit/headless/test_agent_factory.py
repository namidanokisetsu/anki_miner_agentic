import json
from dataclasses import replace

import pytest

from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig
from anki_miner.config import AnkiMinerConfig, ChainEntry, FreqEntry
from anki_miner.gui.utils.config_manager import GUIConfigManager
from anki_miner.runtime.agent_factory import _mining_config, load_agent_config


def test_runtime_overrides_are_narrow_and_path_typed(tmp_path):
    config = _mining_config({"ffmpeg_location": str(tmp_path / "ffmpeg")})
    assert config.ffmpeg_location == tmp_path / "ffmpeg"

    with pytest.raises(AgentMiningError, match="Unknown mining configuration fields"):
        _mining_config({"audio_padding": 0.25})


def test_agent_config_rejects_string_boolean_and_unknown_fields():
    base = {
        "knowledge_sources": [
            {"deck": "Known", "note_type": "ExampleNote", "word_fields": ["word"]}
        ],
        "write_target": {"deck": "Mining", "note_type": "ExampleNote", "enabled": "false"},
    }
    with pytest.raises(AgentMiningError, match="enabled must be boolean"):
        AgentProfileConfig.from_dict(base)

    base["write_target"]["enabled"] = False
    base["max_card"] = 10
    with pytest.raises(AgentMiningError, match="Unknown agent configuration fields"):
        AgentProfileConfig.from_dict(base)


def test_agent_inherits_gui_mining_settings_and_runtime_overrides_win(tmp_path, monkeypatch):
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
                },
                "runtime_overrides": {"ffmpeg_location": str(tmp_path / "ffmpeg")},
            }
        ),
        encoding="utf-8",
    )

    _storage, _profile, mining = load_agent_config(path)

    assert mining.audio_padding == 0.75
    assert mining.ffmpeg_location == tmp_path / "ffmpeg"
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

    with pytest.raises(AgentMiningError, match="Mining policy now comes from the active GUI profile"):
        load_agent_config(path)


def test_legacy_page_size_is_ignored_and_policy_flags_are_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(GUIConfigManager, "load_config", classmethod(lambda cls: AnkiMinerConfig()))
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "knowledge_sources": [
                        {"deck": "Known", "note_type": "ExampleNote", "word_fields": ["word"]}
                    ],
                    "write_target": {"deck": "Mining", "note_type": "ExampleNote"},
                    "page_size": 1,
                    "exclude_katakana_only": True,
                    "exclude_names": False,
                    "exclude_known": False,
                }
            }
        ),
        encoding="utf-8",
    )

    _storage, profile, mining = load_agent_config(path)

    assert not hasattr(profile, "page_size")
    assert mining.exclude_katakana_only_words is True
    assert mining.excluded_wordsets == ()
    assert mining.include_known_words is True
