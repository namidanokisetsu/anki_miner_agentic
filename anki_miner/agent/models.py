"""Versioned public and configuration models for agent mining."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import AgentMiningError, require

PUBLIC_SCHEMA_VERSION = 1


def _reject_unknown(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    require(not unknown, "invalid_config", f"Unknown {context} fields", fields=unknown)


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    require(isinstance(value, (list, tuple)), "invalid_config", f"{field_name} must be an array")
    require(
        all(isinstance(item, str) for item in value),
        "invalid_config",
        f"{field_name} entries must be strings",
    )
    return tuple(value)


def _is_audio_track(value: object, *, allow_none: bool) -> bool:
    return (allow_none and value is None) or value == "japanese" or (type(value) is int and value >= 0)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_id(prefix: str, value: Any, length: int = 32) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class KnowledgeSource:
    deck: str
    note_type: str
    word_fields: tuple[str, ...] = ()
    text_fields: tuple[str, ...] = ()
    ignored_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("word_fields", "text_fields", "ignored_fields"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))
        require(bool(self.deck.strip()), "invalid_mapping", "Knowledge-source deck cannot be empty")
        require(bool(self.note_type.strip()), "invalid_mapping", "Knowledge-source note type cannot be empty")
        require(
            bool(self.word_fields or self.text_fields),
            "invalid_mapping",
            "A knowledge source must select at least one word or text field",
            note_type=self.note_type,
        )
        roles = [*self.word_fields, *self.text_fields, *self.ignored_fields]
        duplicates = sorted({name for name in roles if roles.count(name) > 1})
        require(
            not duplicates,
            "conflicting_field_roles",
            "A field cannot have more than one role",
            note_type=self.note_type,
            fields=duplicates,
        )
        require(
            all(bool(name.strip()) for name in roles),
            "invalid_mapping",
            "Field names cannot be empty",
            note_type=self.note_type,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> KnowledgeSource:
        _reject_unknown(
            value,
            {"deck", "note_type", "word_fields", "text_fields", "ignored_fields"},
            "knowledge source",
        )
        return cls(
            deck=str(value.get("deck", "")),
            note_type=str(value.get("note_type", "")),
            word_fields=_string_tuple(value.get("word_fields", ()), "word_fields"),
            text_fields=_string_tuple(value.get("text_fields", ()), "text_fields"),
            ignored_fields=_string_tuple(value.get("ignored_fields", ()), "ignored_fields"),
        )


@dataclass(frozen=True)
class WriteTarget:
    deck: str
    note_type: str
    enabled: bool = False

    def __post_init__(self) -> None:
        require(bool(self.deck.strip()), "invalid_write_target", "Write-target deck cannot be empty")
        require(bool(self.note_type.strip()), "invalid_write_target", "Write-target note type cannot be empty")
        require(type(self.enabled) is bool, "invalid_config", "write_target.enabled must be boolean")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WriteTarget:
        _reject_unknown(value, {"deck", "note_type", "enabled"}, "write target")
        return cls(
            deck=str(value.get("deck", "")),
            note_type=str(value.get("note_type", "")),
            enabled=value.get("enabled", False),
        )


@dataclass(frozen=True)
class AgentProfileConfig:
    knowledge_sources: tuple[KnowledgeSource, ...]
    write_target: WriteTarget
    mature_interval_days: int = 21
    max_cards: int = 20
    max_payload_bytes: int = 512_000
    max_variants: int = 4
    max_rationale_chars: int = 500
    max_definition_options: int = 12
    max_definition_option_chars: int = 2_000
    max_chosen_definition_chars: int = 240
    max_sentence_translation_chars: int = 500
    chosen_definition_field: str = ""
    sentence_translation_field: str = ""
    # "japanese" uses the existing language-tag auto-detection. An integer is
    # a zero-based index within audio-only streams, matching the GUI override.
    audio_track: Literal["japanese"] | int = "japanese"

    def __post_init__(self) -> None:
        if isinstance(self.knowledge_sources, list):
            object.__setattr__(self, "knowledge_sources", tuple(self.knowledge_sources))
        require(bool(self.knowledge_sources), "invalid_config", "At least one knowledge source is required")
        require(self.mature_interval_days >= 1, "invalid_config", "mature_interval_days must be positive")
        require(self.max_cards >= 1, "invalid_config", "max_cards must be positive")
        require(self.max_payload_bytes >= 1024, "invalid_config", "max_payload_bytes must be at least 1024")
        require(1 <= self.max_variants <= 20, "invalid_config", "max_variants must be between 1 and 20")
        require(
            0 <= self.max_rationale_chars <= 10_000,
            "invalid_config",
            "max_rationale_chars must be between 0 and 10000",
        )
        require(
            1 <= self.max_definition_options <= 50,
            "invalid_config",
            "max_definition_options must be between 1 and 50",
        )
        require(
            100 <= self.max_definition_option_chars <= 10_000,
            "invalid_config",
            "max_definition_option_chars must be between 100 and 10000",
        )
        require(
            1 <= self.max_chosen_definition_chars <= 1_000,
            "invalid_config",
            "max_chosen_definition_chars must be between 1 and 1000",
        )
        require(
            1 <= self.max_sentence_translation_chars <= 2_000,
            "invalid_config",
            "max_sentence_translation_chars must be between 1 and 2000",
        )
        enrichment_fields = [self.chosen_definition_field, self.sentence_translation_field]
        require(
            all(value == value.strip() for value in enrichment_fields),
            "invalid_config",
            "Enrichment field names cannot have surrounding whitespace",
        )
        require(
            not self.chosen_definition_field
            or not self.sentence_translation_field
            or self.chosen_definition_field != self.sentence_translation_field,
            "conflicting_field_roles",
            "Chosen-definition and sentence-translation outputs must use different Anki fields",
        )
        require(
            _is_audio_track(self.audio_track, allow_none=False),
            "invalid_config",
            "audio_track must be 'japanese' or a zero-based audio-track index",
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentProfileConfig:
        allowed = {
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
        _reject_unknown(value, allowed, "agent configuration")
        try:
            sources = tuple(KnowledgeSource.from_dict(item) for item in value["knowledge_sources"])
            target = WriteTarget.from_dict(value["write_target"])
        except (KeyError, TypeError) as exc:
            raise AgentMiningError("invalid_config", "knowledge_sources and write_target are required") from exc
        kwargs = {
            name: value[name]
            for name in (
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
            )
            if name in value
        }
        return cls(knowledge_sources=sources, write_target=target, **kwargs)

    def material_hash(self) -> str:
        value = asdict(self)
        value.pop("max_payload_bytes", None)
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SubtitleSource = Literal["local", "youtube_manual", "youtube_auto", "local_asr"]


@dataclass(frozen=True)
class LocalEpisodeInput:
    video_file: Path
    subtitle_file: Path
    subtitle_source: SubtitleSource = "local"
    episode_id: str | None = None
    audio_track: Literal["japanese"] | int | None = None
    subtitle_offset: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_file", Path(self.video_file).expanduser().resolve())
        object.__setattr__(self, "subtitle_file", Path(self.subtitle_file).expanduser().resolve())
        require(
            self.subtitle_source in {"local", "youtube_manual", "youtube_auto", "local_asr"},
            "invalid_input",
            "Unsupported subtitle source",
            subtitle_source=self.subtitle_source,
        )
        require(
            _is_audio_track(self.audio_track, allow_none=True),
            "invalid_input",
            "audio_track must be null, 'japanese', or a zero-based audio-track index",
        )
        require(
            self.subtitle_offset is None or type(self.subtitle_offset) in {int, float},
            "invalid_input",
            "subtitle_offset must be a number or null",
        )
        if self.subtitle_offset is not None:
            subtitle_offset = float(self.subtitle_offset)
            require(
                -300.0 <= subtitle_offset <= 300.0,
                "invalid_input",
                "subtitle_offset must be between -300 and 300 seconds",
            )
            object.__setattr__(self, "subtitle_offset", subtitle_offset)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LocalEpisodeInput:
        try:
            return cls(
                video_file=Path(value["video_file"]),
                subtitle_file=Path(value["subtitle_file"]),
                subtitle_source=value.get("subtitle_source", "local"),
                episode_id=value.get("episode_id"),
                audio_track=value.get("audio_track"),
                subtitle_offset=value.get("subtitle_offset"),
            )
        except KeyError as exc:
            raise AgentMiningError("invalid_input", "Local input requires video_file and subtitle_file") from exc


@dataclass(frozen=True)
class YouTubeInput:
    url: str
    allow_automatic: bool = True
    allow_asr: bool = False
    episode_id: str | None = None
    audio_track: Literal["japanese"] | int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> YouTubeInput:
        url = str(value.get("url", "")).strip()
        require(bool(url), "invalid_input", "YouTube input requires a URL")
        audio_track = value.get("audio_track")
        allow_automatic = value.get("allow_automatic", True)
        allow_asr = value.get("allow_asr", False)
        require(type(allow_automatic) is bool, "invalid_input", "allow_automatic must be boolean")
        require(type(allow_asr) is bool, "invalid_input", "allow_asr must be boolean")
        require(
            _is_audio_track(audio_track, allow_none=True),
            "invalid_input",
            "audio_track must be null, 'japanese', or a zero-based audio-track index",
        )
        return cls(
            url=url,
            allow_automatic=allow_automatic,
            allow_asr=allow_asr,
            episode_id=value.get("episode_id"),
            audio_track=audio_track,
        )


@dataclass(frozen=True)
class AnalysisToken:
    surface: str
    lexical_id: str
    lemma: str
    reading: str
    pos: str
    subtype: str
    start: int
    end: int

    def validate(self, text: str) -> None:
        require(0 <= self.start < self.end <= len(text), "invalid_token_span", "Token span is outside source text")
        require(
            text[self.start : self.end] == self.surface,
            "invalid_token_span",
            "Token surface does not match its declared span",
            surface=self.surface,
            span=[self.start, self.end],
        )


@dataclass(frozen=True)
class AnalyzerIdentity:
    contract_version: int
    backend: str
    dictionary: str

    @property
    def key(self) -> str:
        return content_id("analyzer", asdict(self))
