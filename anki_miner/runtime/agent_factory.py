"""Qt-window-free composition for learner-aware mining."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from anki_miner.agent.analyzer import SubtitleParserJapaneseAnalyzer
from anki_miner.agent.application import AgentMiningApplication
from anki_miner.agent.candidates import CandidateBatchService
from anki_miner.agent.commit import CandidateWriter, ExistingPipelineCandidateWriter, MiningCommitService
from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig, canonical_json
from anki_miner.agent.policy import effective_policy_inspection, serialized_gui_policy
from anki_miner.agent.profile import LearnerProfileService, ProfileAnkiGateway
from anki_miner.agent.store import AgentStore
from anki_miner.config import AnkiMinerConfig
from anki_miner.config.paths import ANKI_MINER_HOME
from anki_miner.gui.utils.config_manager import GUIConfigManager
from anki_miner.presenters.null_presenter import NullPresenter


def _policy_fingerprint(config: AnkiMinerConfig) -> str:
    serialized = serialized_gui_policy(config)
    return hashlib.sha256(canonical_json(serialized).encode("utf-8")).hexdigest()


def _unsupported(key: str, owner: str) -> None:
    raise AgentMiningError(
        "unsupported_agent_config_key",
        f"Agent configuration key {key!r} is unsupported; configure it in {owner}",
        {
            "key": key,
            "owner": owner,
            "action": f"Remove {key!r} from the configured agent JSON file",
        },
    )


def _mining_config(value: dict[str, Any], *, defaults: AnkiMinerConfig | None = None) -> AnkiMinerConfig:
    """Reject all agent-side mining/runtime shadows; retained for callers/tests."""
    if value:
        _unsupported(sorted(value)[0], "the active GUI profile")
    return defaults or AnkiMinerConfig()


def load_agent_config(path: Path) -> tuple[Path, AgentProfileConfig, AnkiMinerConfig]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentMiningError("invalid_config", f"Cannot read agent configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentMiningError("invalid_config", "Agent configuration root must be an object")
    root_fields = {"storage_path", "agent"}
    if "agent" in raw:
        unknown = sorted(set(raw) - root_fields)
        if unknown:
            key = unknown[0]
            nested = raw.get(key)
            exact = f"{key}.{sorted(nested)[0]}" if isinstance(nested, dict) and nested else key
            _unsupported(exact, "the active GUI profile or normal application configuration")
        profile_raw = raw["agent"]
    else:
        profile_raw = {key: value for key, value in raw.items() if key != "storage_path"}
    if not isinstance(profile_raw, dict):
        raise AgentMiningError("invalid_config", "agent configuration must be an object")
    profile_raw = dict(profile_raw)
    removed = {
        "page_size": "Anki Miner internal transport",
        "review_pool_size": "Anki Miner internal review batching",
        "exclude_katakana_only": "Settings → Filtering",
        "exclude_names": "Settings → Filtering",
        "exclude_known": "Settings → Filtering",
        "blacklist": "Settings → Filtering",
        "whitelist": "Settings → Filtering",
    }
    for key in profile_raw:
        if key in removed:
            _unsupported(key, removed[key])
    allowed_agent = {
        "knowledge_sources",
        "write_target",
        "mature_interval_days",
        "max_cards",
        "max_payload_bytes",
        "max_variants",
        "max_rationale_chars",
        "max_definition_options",
        "max_definition_option_chars",
        "max_chosen_definition_chars",
        "max_sentence_translation_chars",
        "chosen_definition_field",
        "sentence_translation_field",
        "audio_track",
    }
    unknown_agent = sorted(set(profile_raw) - allowed_agent)
    if unknown_agent:
        _unsupported(unknown_agent[0], "the agent configuration schema")
    sources = profile_raw.get("knowledge_sources")
    if isinstance(sources, list):
        allowed_source = {"deck", "note_type", "word_fields", "text_fields", "ignored_fields"}
        for index, source in enumerate(sources):
            if isinstance(source, dict) and (unknown_source := sorted(set(source) - allowed_source)):
                _unsupported(
                    f"knowledge_sources[{index}].{unknown_source[0]}",
                    "the agent knowledge-source schema",
                )
    target = profile_raw.get("write_target")
    if isinstance(target, dict) and (unknown_target := sorted(set(target) - {"deck", "note_type", "enabled"})):
        _unsupported(f"write_target.{unknown_target[0]}", "the agent write-target schema")
    profile = AgentProfileConfig.from_dict(profile_raw)
    mining = GUIConfigManager.load_config()
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
    policy_fingerprint = _policy_fingerprint(mining_config)
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
        word_list_service=processor.word_list_service,
        definition_probe=definition_probe,
        definition_options_lookup=processor.definition_service.lookup_all_offline,
        asr_generator=generate_asr,
        frequency_service=processor.frequency_service,
        pitch_service=processor.pitch_accent_service,
        mining_policy=mining_config,
        mining_policy_hash=policy_fingerprint,
    )
    effective_writer = writer if writer is not None else ExistingPipelineCandidateWriter(processor)
    commit_service = MiningCommitService(store, profile_config, effective_writer)
    return AgentMiningApplication(
        store,
        profile_config,
        profile_service,
        candidate_service,
        commit_service,
        mining_policy_info=effective_policy_inspection(mining_config, policy_fingerprint),
        close_callback=processor.close,
    )
