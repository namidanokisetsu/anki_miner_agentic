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
from anki_miner.agent.profile import LearnerProfileService, ProfileAnkiGateway
from anki_miner.agent.store import AgentStore
from anki_miner.config import AnkiMinerConfig
from anki_miner.config.paths import ANKI_MINER_HOME
from anki_miner.gui.utils.config_manager import GUIConfigManager
from anki_miner.presenters.null_presenter import NullPresenter


_RUNTIME_OVERRIDE_FIELDS = {
    "alass_location",
    "ffmpeg_location",
    "ffprobe_location",
    "youtube_ffmpeg_location",
    "ytdlp_location",
}


def _policy_fingerprint(config: AnkiMinerConfig) -> str:
    serialized = GUIConfigManager._paths_to_strings(GUIConfigManager._config_to_serializable_dict(config))
    return hashlib.sha256(canonical_json(serialized).encode("utf-8")).hexdigest()


def _mining_config(value: dict[str, Any], *, defaults: AnkiMinerConfig | None = None) -> AnkiMinerConfig:
    """Apply the narrow set of headless runtime overrides to GUI mining policy."""
    defaults = defaults or AnkiMinerConfig()
    unknown = sorted(set(value) - _RUNTIME_OVERRIDE_FIELDS)
    if unknown:
        raise AgentMiningError("invalid_config", "Unknown mining configuration fields", {"fields": unknown})
    converted: dict[str, Any] = {}
    try:
        for name, raw in value.items():
            if raw is not None and not isinstance(raw, (str, Path)):
                raise TypeError(f"{name} must be a path string or null")
            converted[name] = Path(raw).expanduser() if raw else None
        return replace(defaults, **converted)
    except (TypeError, ValueError) as exc:
        raise AgentMiningError("invalid_config", f"Invalid mining configuration: {exc}") from exc


def load_agent_config(path: Path) -> tuple[Path, AgentProfileConfig, AnkiMinerConfig]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentMiningError("invalid_config", f"Cannot read agent configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentMiningError("invalid_config", "Agent configuration root must be an object")
    root_fields = {"storage_path", "agent", "runtime_overrides", "mining"}
    if "agent" in raw:
        unknown = sorted(set(raw) - root_fields)
        if unknown:
            raise AgentMiningError("invalid_config", "Unknown configuration fields", {"fields": unknown})
        profile_raw = raw["agent"]
    else:
        profile_raw = {
            key: value for key, value in raw.items() if key not in {"storage_path", "mining", "runtime_overrides"}
        }
    if not isinstance(profile_raw, dict):
        raise AgentMiningError("invalid_config", "agent configuration must be an object")
    profile_raw = dict(profile_raw)
    profile_raw.pop("page_size", None)  # Legacy internal tuning; safely ignored.
    legacy_policy = {
        key: profile_raw.pop(key)
        for key in ("exclude_katakana_only", "exclude_names", "exclude_known", "blacklist", "whitelist")
        if key in profile_raw
    }
    for key in ("exclude_katakana_only", "exclude_names", "exclude_known"):
        if key in legacy_policy and type(legacy_policy[key]) is not bool:
            raise AgentMiningError("invalid_config", f"Legacy {key} must be boolean")
    if legacy_policy.get("blacklist") or legacy_policy.get("whitelist"):
        raise AgentMiningError(
            "invalid_config",
            "Legacy inline word lists must be moved to the active GUI profile",
            {"fields": [key for key in ("blacklist", "whitelist") if legacy_policy.get(key)]},
        )
    profile = AgentProfileConfig.from_dict(profile_raw)
    mining = GUIConfigManager.load_config()
    legacy_mining = raw.get("mining", {})
    runtime_overrides = raw.get("runtime_overrides", {})
    if not isinstance(legacy_mining, dict) or not isinstance(runtime_overrides, dict):
        raise AgentMiningError("invalid_config", "runtime_overrides must be an object")
    moved_policy = sorted(set(legacy_mining) - _RUNTIME_OVERRIDE_FIELDS)
    if moved_policy:
        raise AgentMiningError(
            "invalid_config",
            "Mining policy now comes from the active GUI profile",
            {"fields": moved_policy},
        )
    conflicts = sorted(set(legacy_mining) & set(runtime_overrides))
    if conflicts:
        raise AgentMiningError("invalid_config", "Conflicting runtime overrides", {"fields": conflicts})
    mining = _mining_config(legacy_mining | runtime_overrides, defaults=mining)
    legacy_updates: dict[str, Any] = {}
    if "exclude_katakana_only" in legacy_policy:
        legacy_updates["exclude_katakana_only_words"] = bool(legacy_policy["exclude_katakana_only"])
    if legacy_policy.get("exclude_names") is False:
        legacy_updates["excluded_wordsets"] = ()
    if "exclude_known" in legacy_policy:
        legacy_updates["include_known_words"] = not bool(legacy_policy["exclude_known"])
    if legacy_updates:
        mining = replace(mining, **legacy_updates)
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
        mining_policy_info={"source": "active_gui_profile", "fingerprint": policy_fingerprint},
        close_callback=processor.close,
    )
