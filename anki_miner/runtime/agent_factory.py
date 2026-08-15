"""Qt-window-free composition for learner-aware mining."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from anki_miner.agent.analyzer import SubtitleParserJapaneseAnalyzer
from anki_miner.agent.application import AgentMiningApplication
from anki_miner.agent.candidates import CandidateBatchService
from anki_miner.agent.commit import CandidateWriter, ExistingPipelineCandidateWriter, MiningCommitService
from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig
from anki_miner.agent.profile import LearnerProfileService, ProfileAnkiGateway
from anki_miner.agent.store import AgentStore
from anki_miner.config import AnkiMinerConfig, AudioSourceEntry, ChainEntry, FreqEntry, PitchSourceEntry
from anki_miner.config.paths import ANKI_MINER_HOME
from anki_miner.gui.utils.config_manager import GUIConfigManager
from anki_miner.presenters.null_presenter import NullPresenter


def _mining_config(value: dict[str, Any], *, defaults: AnkiMinerConfig | None = None) -> AnkiMinerConfig:
    defaults = defaults or AnkiMinerConfig()
    allowed = {item.name for item in fields(AnkiMinerConfig)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AgentMiningError("invalid_config", "Unknown mining configuration fields", {"fields": unknown})
    converted: dict[str, Any] = {}
    for name, raw in value.items():
        current = getattr(defaults, name)
        if name == "dictionary_chain":
            converted[name] = _configuration_chain(raw, ChainEntry, name)
        elif name == "frequency_chain":
            converted[name] = _configuration_chain(raw, FreqEntry, name)
        elif name == "pitch_chain":
            converted[name] = _configuration_chain(raw, PitchSourceEntry, name)
        elif name == "expression_audio_chain":
            converted[name] = _configuration_chain(raw, AudioSourceEntry, name)
        elif isinstance(current, Path):
            converted[name] = Path(raw).expanduser()
        elif isinstance(current, tuple) and isinstance(raw, list):
            converted[name] = tuple(raw)
        else:
            converted[name] = raw
    try:
        return replace(defaults, **converted)
    except (TypeError, ValueError) as exc:
        raise AgentMiningError("invalid_config", f"Invalid mining configuration: {exc}") from exc


def _configuration_chain(raw: Any, entry_type: type[Any], field_name: str) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise AgentMiningError("invalid_config", f"{field_name} must be an array")
    entries: list[Any] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AgentMiningError("invalid_config", f"{field_name}[{index}] must be an object")
        try:
            entry = entry_type(**item)
        except TypeError as exc:
            raise AgentMiningError("invalid_config", f"Invalid {field_name}[{index}]: {exc}") from exc
        if type(entry.enabled) is not bool:
            raise AgentMiningError("invalid_config", f"{field_name}[{index}].enabled must be boolean")
        if isinstance(entry, ChainEntry):
            if entry.kind not in {"indexed", "jisho"} or (entry.kind == "indexed" and not entry.dict_id):
                raise AgentMiningError("invalid_config", f"Invalid dictionary source at {field_name}[{index}]")
        elif isinstance(entry, AudioSourceEntry):
            if entry.kind not in {"pack", "jpod101", "googletts", "custom", "custom_json"}:
                raise AgentMiningError("invalid_config", f"Invalid audio source at {field_name}[{index}]")
        elif not entry.source_id:
            raise AgentMiningError("invalid_config", f"{field_name}[{index}].source_id cannot be empty")
        entries.append(entry)
    return tuple(entries)


def load_agent_config(path: Path) -> tuple[Path, AgentProfileConfig, AnkiMinerConfig]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentMiningError("invalid_config", f"Cannot read agent configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentMiningError("invalid_config", "Agent configuration root must be an object")
    root_fields = {"storage_path", "agent", "mining"}
    if "agent" in raw:
        unknown = sorted(set(raw) - root_fields)
        if unknown:
            raise AgentMiningError("invalid_config", "Unknown configuration fields", {"fields": unknown})
        profile_raw = raw["agent"]
    else:
        profile_raw = {key: value for key, value in raw.items() if key not in {"storage_path", "mining"}}
    if not isinstance(profile_raw, dict):
        raise AgentMiningError("invalid_config", "agent configuration must be an object")
    profile = AgentProfileConfig.from_dict(profile_raw)
    # GUI settings are the shared mining defaults. Explicit values in the
    # agent file remain deliberate per-agent overrides.
    mining = _mining_config(raw.get("mining", {}), defaults=GUIConfigManager.load_config())
    storage = Path(raw.get("storage_path", ANKI_MINER_HOME / "agent_mining.sqlite3")).expanduser().resolve()
    mining = replace(
        mining,
        anki_deck_name=profile.write_target.deck,
        anki_note_type=profile.write_target.note_type,
        anki_fields={
            **dict(mining.anki_fields),
            "chosen_definition": profile.chosen_definition_field,
            "sentence_translation": profile.sentence_translation_field,
        },
    )
    return storage, profile, mining


def build_agent_application(
    config_path: Path,
    *,
    gateway: ProfileAnkiGateway | None = None,
    writer: CandidateWriter | None = None,
) -> AgentMiningApplication:
    storage_path, profile_config, mining_config = load_agent_config(config_path)
    store = AgentStore(storage_path)

    # This factory is window-free. The existing service composition imports Qt
    # translation primitives but never constructs QApplication or a widget.
    from anki_miner.gui.utils.service_factory import create_episode_processor

    processor = create_episode_processor(mining_config, NullPresenter())
    if gateway is None:
        gateway = processor.anki_service
    analyzer = SubtitleParserJapaneseAnalyzer(processor.subtitle_parser)
    profile_service = LearnerProfileService(store, analyzer, gateway, profile_config)
    definition_probe = processor.definition_service.has_offline_definitions

    def generate_asr(video_file: Path, subtitle_file: Path, audio_track_override: int | None) -> Path:
        from anki_miner.services.asr.subtitle_generation import SubtitleGenStatus, generate_subtitle_one

        result = generate_subtitle_one(
            mining_config,
            processor.media_extractor,
            video_file,
            subtitle_file,
            audio_track_override=audio_track_override,
        )
        if result.status is not SubtitleGenStatus.SUCCESS or result.out_srt is None:
            raise AgentMiningError(
                "asr_failed",
                "Local ASR could not produce subtitles",
                {"status": result.status.name.lower(), "video_file": str(video_file)},
            )
        return result.out_srt

    candidate_service = CandidateBatchService(
        store,
        analyzer,
        processor.subtitle_parser,
        processor.word_filter,
        profile_config,
        youtube_fetcher=processor._youtube_fetcher,
        wordset_service=processor.wordset_service,
        definition_probe=definition_probe,
        definition_options_lookup=processor.definition_service.lookup_all_offline,
        asr_generator=generate_asr,
        frequency_service=processor.frequency_service,
        pitch_service=processor.pitch_accent_service,
        mining_policy=mining_config,
    )
    effective_writer = writer if writer is not None else ExistingPipelineCandidateWriter(processor)
    commit_service = MiningCommitService(store, profile_config, effective_writer)
    return AgentMiningApplication(
        store,
        profile_config,
        profile_service,
        candidate_service,
        commit_service,
        close_callback=processor.close,
    )
