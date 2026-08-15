"""Side-effect-free episode analysis and immutable candidate preparation."""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter, defaultdict
from dataclasses import fields
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Literal

from anki_miner.models import TokenizedWord
from anki_miner.models.youtube import SubMode
from anki_miner.services.frequency.multi_frequency_service import harmonic_rank, min_rank
from anki_miner.services.subtitle_parser import _differs_by_okurigana_only
from anki_miner.utils.text_utils import is_katakana_only, katakana_to_hiragana

from .analyzer import JapaneseAnalyzer
from .errors import AgentMiningError, require
from .models import (
    PUBLIC_SCHEMA_VERSION,
    AgentProfileConfig,
    LocalEpisodeInput,
    YouTubeInput,
    canonical_json,
    content_id,
)
from .store import AgentStore

CANDIDATE_CONTRACT_VERSION = 2

DefinitionProbe = Callable[[list[str]], dict[str, bool] | None]
DefinitionOptionsLookup = Callable[[str], list[tuple[str, str]]]
AsrGenerator = Callable[[Path, Path, int | None], Path]


class _DefinitionTextExtractor(HTMLParser):
    """Reduce rendered dictionary HTML to compact, model-readable text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _compact_definition_options(
    entries: list[tuple[str, str]], *, max_options: int, max_chars: int
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_dictionary, entry_html in entries:
        parser = _DefinitionTextExtractor()
        parser.feed(entry_html)
        text = " ".join(" ".join(parser.parts).split())
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        dictionary = " ".join(raw_dictionary.split())[:200]
        if not dictionary or not text:
            continue
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        identity = (dictionary, text)
        if identity in seen:
            continue
        seen.add(identity)
        options.append({"dictionary": dictionary, "text": text})
        if len(options) >= max_options:
            break
    return options


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    require(path.is_file(), "missing_input", "Input file does not exist", path=str(path))
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": _sha256_file(path)}


def _serialize_word(word: TokenizedWord) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in fields(TokenizedWord):
        if item.name in {"sentence_candidates", "video_file"}:
            continue
        value = getattr(word, item.name)
        result[item.name] = value
    result["mined_form"] = word.mined_form
    return result


class CandidateBatchService:
    def __init__(
        self,
        store: AgentStore,
        analyzer: JapaneseAnalyzer,
        parser: Any,
        word_filter: Any,
        config: AgentProfileConfig,
        *,
        youtube_fetcher: Any = None,
        wordset_service: Any = None,
        definition_probe: DefinitionProbe | None = None,
        definition_options_lookup: DefinitionOptionsLookup | None = None,
        asr_generator: AsrGenerator | None = None,
        frequency_service: Any = None,
        pitch_service: Any = None,
        mining_policy: Any = None,
    ) -> None:
        self.store = store
        self.analyzer = analyzer
        self.parser = parser
        self.word_filter = word_filter
        self.config = config
        self.youtube_fetcher = youtube_fetcher
        self.wordset_service = wordset_service
        self.definition_probe = definition_probe
        self.definition_options_lookup = definition_options_lookup
        self.asr_generator = asr_generator
        self.frequency_service = frequency_service
        self.pitch_service = pitch_service
        self.mining_policy = mining_policy

    def prepare(
        self,
        inputs: list[LocalEpisodeInput | YouTubeInput],
        *,
        max_cards: int | None = None,
        review_pool_size: int | None = None,
    ) -> dict[str, Any]:
        require(bool(inputs), "invalid_input", "At least one episode input is required")
        profile = self.store.profile_status()
        require(profile["status"] == "ready", "profile_missing", "Synchronize the learner profile first")
        require(
            profile["analyzer_key"] == self.analyzer.identity.key,
            "incompatible_analyzer",
            "The learner profile was built by an incompatible analyzer",
            profile_analyzer=profile["analyzer_key"],
            current_analyzer=self.analyzer.identity.key,
        )
        effective_max_cards = self.config.max_cards if max_cards is None else max_cards
        effective_pool = self.config.review_pool_size if review_pool_size is None else review_pool_size
        require(1 <= effective_max_cards <= self.config.max_cards, "invalid_limit", "max_cards exceeds the profile cap")
        require(
            effective_pool is None or effective_pool >= 1, "invalid_limit", "review_pool_size must be positive or null"
        )

        resolved = [self._resolve_input(item) for item in inputs]
        episode_parsers = [self._parser_for_episode(item) for item in resolved]
        sources = [self._source_record(item, parser) for item, parser in zip(resolved, episode_parsers, strict=True)]
        known = self.store.lexical_features()
        occurrences: Counter[str] = Counter()
        variants: dict[str, list[tuple[TokenizedWord, dict[str, Any], tuple[str, ...]]]] = defaultdict(list)

        for episode, source, parser in zip(resolved, sources, episode_parsers, strict=True):
            words, line_index = parser.parse_subtitle_file_with_index(episode.subtitle_file)
            counts = parser.count_lemmas(episode.subtitle_file)
            self._attach_frequency(words)
            self.word_filter.attach_occurrence_counts(words, counts)
            self.word_filter.attach_sentence_candidates(words, line_index, max_candidates=self.config.max_variants)
            cue_flags = self._cue_flags(episode, parser)
            for word in words:
                word.video_file = episode.video_file
                occurrences[word.mined_form] += max(1, word.occurrence_count)
                key = (round(word.start_time, 3), round(word.end_time, 3), word.sentence)
                flags = cue_flags.get(key, ())
                variants[word.mined_form].append((word, source, flags))

        definition_terms = sorted(variants)
        definition_map = self.definition_probe(definition_terms) if self.definition_probe else None
        pitch_map = self._lookup_pitch(variants)
        definition_options: dict[str, list[dict[str, str]]] = {}
        if self.definition_options_lookup is not None and self.config.chosen_definition_field:
            for term in definition_terms:
                if definition_map is not None and not definition_map.get(term, False):
                    continue
                definition_options[term] = _compact_definition_options(
                    self.definition_options_lookup(term),
                    max_options=self.config.max_definition_options,
                    max_chars=self.config.max_definition_option_chars,
                )
        lookup_material = {
            "definitions": sorted(definition_map.items()) if definition_map is not None else None,
            "definition_options": sorted(definition_options.items()) if definition_options else None,
            "pitch": sorted(pitch_map.items()) if pitch_map is not None else None,
            "frequency": sorted(
                (
                    lexical_id,
                    choices[0][0].frequency_rank,
                    choices[0][0].frequency_sources,
                )
                for lexical_id, choices in variants.items()
            ),
        }
        request_material = {
            "candidate_contract_version": CANDIDATE_CONTRACT_VERSION,
            "sources": sources,
            "profile_revision_id": profile["revision_id"],
            "analyzer_key": self.analyzer.identity.key,
            "config_hash": self.config.material_hash(),
            "lookup_material": lookup_material,
            "max_cards": effective_max_cards,
            "review_pool_size": effective_pool,
        }
        request_hash = hashlib.sha256(canonical_json(request_material).encode("utf-8")).hexdigest()
        revision_id = content_id("batch", request_material)
        candidates: list[dict[str, Any]] = []
        allowed_enrichments = [
            key
            for key, configured_field in (
                ("chosen_definition", self.config.chosen_definition_field),
                ("sentence_translation", self.config.sentence_translation_field),
            )
            if configured_field
        ]
        eligible_sentences: set[str] = set()
        for lexical_id, choices in variants.items():
            primary, source, quality_flags = choices[0]
            learner = known.get(
                lexical_id,
                {
                    "state": "unseen",
                    "word_exposures": 0,
                    "sentence_exposures": 0,
                    "word_card_count": 0,
                    "reviews": 0,
                    "lapses": 0,
                    "interval_days": None,
                },
            )
            reasons: list[dict[str, str]] = []
            whitelisted = lexical_id in self.config.whitelist or primary.lemma in self.config.whitelist
            if not whitelisted and (lexical_id in self.config.blacklist or primary.lemma in self.config.blacklist):
                reasons.append({"code": "blacklisted", "message": "Target is on the configured blacklist"})
            if not whitelisted and self.config.exclude_katakana_only and is_katakana_only(lexical_id):
                reasons.append({"code": "katakana_only", "message": "Target is written only in katakana"})
            if (
                not whitelisted
                and self.config.exclude_names
                and self.wordset_service is not None
                and self.wordset_service.is_available()
                and self.wordset_service.is_excluded(lexical_id)
            ):
                reasons.append({"code": "known_name", "message": "Target matches an enabled name wordset"})
            if (
                not whitelisted
                and self.config.exclude_known
                and (learner["word_exposures"] > 0 or learner["word_card_count"] > 0)
            ):
                reasons.append({"code": "already_known", "message": "Target has explicit word-field or card evidence"})
            definition_available = definition_map.get(lexical_id, False) if definition_map is not None else None
            if definition_map is not None and not definition_available:
                reasons.append({"code": "no_definition", "message": "No offline dictionary definition is available"})

            policy = self.mining_policy
            if not whitelisted and policy is not None:
                low = int(getattr(policy, "min_frequency_rank", 0) or 0)
                high = int(getattr(policy, "max_frequency_rank", 0) or 0)
                rank = primary.frequency_rank
                if (low or high) and rank is None and not bool(getattr(policy, "frequency_keep_unranked", False)):
                    reasons.append(
                        {"code": "frequency_unranked", "message": "Target has no rank in the active frequency band"}
                    )
                elif rank is not None and low and rank < low:
                    reasons.append(
                        {"code": "frequency_too_common", "message": "Target is more common than the configured band"}
                    )
                elif rank is not None and high and rank > high:
                    reasons.append(
                        {"code": "frequency_too_rare", "message": "Target is rarer than the configured band"}
                    )
                if bool(getattr(policy, "use_sentence_length_filter", False)):
                    max_duration = float(getattr(policy, "max_sentence_duration_seconds", 0.0) or 0.0)
                    max_chars = int(getattr(policy, "max_sentence_chars", 0) or 0)
                    if max_duration and primary.duration > max_duration:
                        reasons.append({"code": "sentence_too_long", "message": "Sentence exceeds the duration limit"})
                    if max_chars and len(primary.sentence) > max_chars:
                        reasons.append({"code": "sentence_too_long", "message": "Sentence exceeds the character limit"})

            sentence_lexemes = {
                other_id
                for other_id, other_choices in variants.items()
                if any(item[0].sentence == primary.sentence for item in other_choices)
            }
            unknown_count = sum(
                1
                for item in sentence_lexemes
                if known.get(item, {"word_exposures": 0, "word_card_count": 0})["word_exposures"] == 0
                and known.get(item, {"word_card_count": 0})["word_card_count"] == 0
            )
            if (
                not whitelisted
                and policy is not None
                and bool(getattr(policy, "use_i_plus_one_filter", False))
                and unknown_count != 1
            ):
                reasons.append(
                    {"code": "not_i_plus_one", "message": "Sentence does not contain exactly one unknown lexeme"}
                )
            sentence_key = " ".join(primary.sentence.split())
            if (
                not whitelisted
                and not reasons
                and policy is not None
                and bool(getattr(policy, "deduplicate_sentences", False))
            ):
                if sentence_key in eligible_sentences:
                    reasons.append(
                        {"code": "duplicate_sentence", "message": "Another eligible target already uses this sentence"}
                    )
                else:
                    eligible_sentences.add(sentence_key)
            candidate_material = {
                "batch_revision": revision_id,
                "lexical_id": lexical_id,
                "source": source["episode_id"],
                "start_ms": round(primary.start_time * 1000),
                "sentence": primary.sentence,
            }
            candidate_id = content_id("candidate", candidate_material)
            alternative_words: list[TokenizedWord] = []
            for word, _variant_source, _flags in choices:
                pool = word.sentence_candidates or [word]
                for variant in pool:
                    if all(
                        (variant.sentence, variant.start_time, variant.end_time)
                        != (seen.sentence, seen.start_time, seen.end_time)
                        for seen in alternative_words
                    ):
                        alternative_words.append(variant)
                    if len(alternative_words) >= self.config.max_variants:
                        break
                if len(alternative_words) >= self.config.max_variants:
                    break
            public = {
                "schema_version": PUBLIC_SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "batch_revision": revision_id,
                "episode": {
                    "id": source["episode_id"],
                    "cue_index": source["cue_indexes"].get(
                        f"{round(primary.start_time, 3)}:{round(primary.end_time, 3)}:{primary.sentence}", 0
                    ),
                    "start_ms": round(primary.start_time * 1000),
                    "end_ms": round(primary.end_time * 1000),
                    "subtitle_source": source["subtitle_source"],
                    "audio_track": source["audio_track"],
                    "subtitle_offset": source["subtitle_offset"],
                },
                "target": {
                    "surface": primary.surface,
                    "mined_form": lexical_id,
                    "reading": primary.expression_reading or primary.reading,
                    "pos": primary.pos,
                },
                "sentence": {
                    "text": primary.sentence,
                    "chars": len(primary.sentence),
                    "duration_ms": round(primary.duration * 1000),
                    "unknown_lexemes": unknown_count,
                },
                "learner": learner,
                "signals": {
                    "frequency_rank": primary.frequency_rank,
                    "episode_occurrences": occurrences[lexical_id],
                    "definition_available": definition_available,
                    "pitch_available": (
                        bool(pitch_map.get(lexical_id, {}).get("position")) if pitch_map is not None else None
                    ),
                    "variant_count": len(alternative_words),
                },
                "pitch": (
                    pitch_map.get(lexical_id, {"position": None, "category": None}) if pitch_map is not None else None
                ),
                "definition_options": definition_options.get(lexical_id, []),
                "allowed_enrichments": allowed_enrichments,
                "flags": list(quality_flags),
                "eligible": not reasons,
                "eligibility": {"reason_codes": [item["code"] for item in reasons], "diagnostics": reasons},
                "variants": [
                    {
                        "sentence": variant.sentence,
                        "start_ms": round(variant.start_time * 1000),
                        "end_ms": round(variant.end_time * 1000),
                    }
                    for variant in alternative_words
                ],
            }
            internal = {
                "word": _serialize_word(primary),
                "video_fingerprint": source["video"],
                "subtitle_fingerprint": source["subtitle"],
                "subtitle_source": source["subtitle_source"],
                "episode_id": source["episode_id"],
                "audio_track": source["audio_track"],
                "subtitle_offset": source["subtitle_offset"],
            }
            candidates.append({"lexical_id": lexical_id, "public": public, "internal": internal})

        candidates.sort(
            key=lambda item: (
                not item["public"]["eligible"],
                item["public"]["signals"]["frequency_rank"] or 10**9,
                -item["public"]["signals"]["episode_occurrences"],
                item["public"]["candidate_id"],
            )
        )
        batch = {
            "revision_id": revision_id,
            "profile_revision_id": profile["revision_id"],
            "analyzer_key": self.analyzer.identity.key,
            "config_hash": self.config.material_hash(),
            "request_hash": request_hash,
            "sources": sources,
            "max_cards": effective_max_cards,
            "review_pool_size": effective_pool,
        }
        return self.store.create_batch(batch, candidates)

    def _attach_frequency(self, words: list[TokenizedWord]) -> None:
        service = self.frequency_service
        if service is None or not service.is_available() or not words:
            return
        pairs = [
            (
                word.mined_form,
                katakana_to_hiragana(word.expression_reading or word.lemma_reading or word.reading) or None,
            )
            for word in words
        ]
        all_sources = service.lookup_all_many(pairs)
        fallback_indexes = [
            index
            for index, (word, sources) in enumerate(zip(words, all_sources, strict=True))
            if not sources
            and word.lemma
            and word.lemma != word.mined_form
            and _differs_by_okurigana_only(word.mined_form, word.lemma)
        ]
        if fallback_indexes:
            fallback_pairs = [
                (
                    words[index].lemma,
                    katakana_to_hiragana(words[index].lemma_reading or words[index].reading) or None,
                )
                for index in fallback_indexes
            ]
            for index, sources in zip(
                fallback_indexes,
                service.lookup_all_many(fallback_pairs),
                strict=True,
            ):
                all_sources[index] = sources
        for word, sources in zip(words, all_sources, strict=True):
            word.frequency_sources = sources
            word.frequency_rank = min_rank(sources)
            word.frequency_harmonic_rank = harmonic_rank(sources)

    def _lookup_pitch(
        self,
        variants: dict[str, list[tuple[TokenizedWord, dict[str, Any], tuple[str, ...]]]],
    ) -> dict[str, dict[str, str | None]] | None:
        service = self.pitch_service
        if service is None or not service.is_available() or not variants:
            return None
        lexical_ids = list(variants)
        words = [variants[lexical_id][0][0] for lexical_id in lexical_ids]
        keys = [
            (
                word.mined_form,
                word.expression_reading or word.resolved_reading or word.lemma_reading or word.reading,
                word.pos,
            )
            for word in words
        ]
        pitch = service.lookup_batch_detailed(
            keys,
            fmt=getattr(self.mining_policy, "pitch_category_format", "jp"),
        )
        fallback_indexes = [
            index
            for index, ((position, _category), word) in enumerate(zip(pitch, words, strict=True))
            if not position
            and word.lemma != word.mined_form
            and _differs_by_okurigana_only(word.mined_form, word.lemma)
        ]
        if fallback_indexes:
            fallback_keys = [
                (
                    words[index].lemma,
                    words[index].resolved_reading or words[index].lemma_reading or words[index].reading,
                    words[index].pos,
                )
                for index in fallback_indexes
            ]
            fallback_pitch = service.lookup_batch_detailed(
                fallback_keys,
                fmt=getattr(self.mining_policy, "pitch_category_format", "jp"),
            )
            for index, fallback in zip(fallback_indexes, fallback_pitch, strict=True):
                if fallback[0]:
                    pitch[index] = fallback
        return {
            lexical_id: {"position": position, "category": category}
            for lexical_id, (position, category) in zip(lexical_ids, pitch, strict=True)
        }

    def _resolve_input(self, item: LocalEpisodeInput | YouTubeInput) -> LocalEpisodeInput:
        if isinstance(item, LocalEpisodeInput):
            return item
        require(self.youtube_fetcher is not None, "youtube_unavailable", "YouTube preparation is not configured")
        info = self.youtube_fetcher.probe_metadata(item.url)
        if info.has_manual_ja_subs:
            mode: SubMode = "manual_only"
        elif info.has_auto_ja_subs and item.allow_automatic:
            mode = "auto_only"
        elif item.allow_asr:
            return self._resolve_youtube_asr(item, info.video_id)
        else:
            raise AgentMiningError(
                "no_japanese_subtitles",
                "No acceptable native Japanese subtitle track is available",
                {"video_id": info.video_id, "automatic_available": info.has_auto_ja_subs},
            )
        workspace = self.store.path.parent / "agent_sources" / info.video_id
        workspace.mkdir(parents=True, exist_ok=True)
        fetched = self.youtube_fetcher.fetch_video(
            item.url,
            info.video_id,
            workspace,
            mode,
            cancel_event=threading.Event(),
            fallback_allowed=info.has_auto_ja_subs,
        )
        source: Literal["youtube_manual", "youtube_auto"] = (
            "youtube_manual" if fetched.sub_source == "manual" else "youtube_auto"
        )
        return LocalEpisodeInput(
            fetched.video_file,
            fetched.subtitle_file,
            source,
            item.episode_id or f"YT:{info.video_id}",
            item.audio_track,
        )

    def _parser_for_episode(self, episode: LocalEpisodeInput) -> Any:
        if episode.subtitle_offset is None:
            return self.parser
        with_offset = getattr(self.parser, "with_subtitle_offset", None)
        require(
            callable(with_offset),
            "invalid_config",
            "The configured subtitle parser does not support per-input offsets",
        )
        assert callable(with_offset)
        return with_offset(episode.subtitle_offset)

    def _resolve_youtube_asr(self, item: YouTubeInput, video_id: str) -> LocalEpisodeInput:
        """Download a YouTube source and transcribe it with the existing local ASR pipeline."""
        asr_generator = self.asr_generator
        require(asr_generator is not None, "asr_unavailable", "Local ASR preparation is not configured")
        assert asr_generator is not None
        audio_policy = item.audio_track if item.audio_track is not None else self.config.audio_track
        audio_track_override = audio_policy if type(audio_policy) is int else None
        workspace = self.store.path.parent / "agent_sources" / video_id
        workspace.mkdir(parents=True, exist_ok=True)
        from anki_miner.exceptions.youtube import NoJapaneseSubtitlesError

        try:
            fetched = self.youtube_fetcher.fetch_video(
                item.url,
                video_id,
                workspace,
                "auto_only",
                cancel_event=threading.Event(),
                fallback_allowed=False,
            )
            video_file = fetched.video_file
        except NoJapaneseSubtitlesError:
            # The existing fetcher deliberately leaves a successfully downloaded
            # video in place when the requested subtitle track is absent.
            video_files = [
                path
                for path in workspace.glob(f"{video_id}*")
                if path.suffix.lower() in {".mp4", ".mkv", ".webm"} and path.is_file()
            ]
            require(
                len(video_files) == 1,
                "youtube_fetch_failed",
                "YouTube download completed without a unique video for ASR",
                video_id=video_id,
                files=[str(path) for path in video_files],
            )
            video_file = video_files[0]

        track_key = f"audio-{audio_track_override}" if audio_track_override is not None else "japanese"
        subtitle_file = workspace / f"{video_id}.agent-asr-{track_key}.srt"
        if not subtitle_file.is_file() or subtitle_file.stat().st_size == 0:
            subtitle_file = asr_generator(video_file, subtitle_file, audio_track_override)
        require(
            subtitle_file.is_file() and subtitle_file.stat().st_size > 0,
            "asr_failed",
            "Local ASR did not produce a usable subtitle file",
            video_id=video_id,
        )
        return LocalEpisodeInput(
            video_file,
            subtitle_file,
            "local_asr",
            item.episode_id or f"YT:{video_id}",
            item.audio_track,
        )

    def _cue_flags(self, episode: LocalEpisodeInput, parser: Any) -> dict[tuple[float, float, str], tuple[str, ...]]:
        from .quality import assess_subtitle_cue

        result: dict[tuple[float, float, str], tuple[str, ...]] = {}
        for start, end, text in parser.parse_raw_entries(episode.subtitle_file):
            quality = assess_subtitle_cue(text, start, end, episode.subtitle_source)
            require(
                quality.severe_error is None,
                "invalid_subtitle_cue",
                "Subtitle contains a structurally invalid cue",
                reason=quality.severe_error,
                start=start,
                end=end,
            )
            result[(round(start, 3), round(end, 3), text)] = quality.flags
        return result

    def _source_record(self, episode: LocalEpisodeInput, parser: Any) -> dict[str, Any]:
        video = file_fingerprint(episode.video_file)
        subtitle = file_fingerprint(episode.subtitle_file)
        entries = parser.parse_raw_entries(episode.subtitle_file)
        cue_indexes = {
            f"{round(start, 3)}:{round(end, 3)}:{text}": index for index, (start, end, text) in enumerate(entries)
        }
        return {
            "episode_id": episode.episode_id or content_id("episode", {"video": video, "subtitle": subtitle}),
            "subtitle_source": episode.subtitle_source,
            "audio_track": episode.audio_track if episode.audio_track is not None else self.config.audio_track,
            "subtitle_offset": (
                episode.subtitle_offset
                if episode.subtitle_offset is not None
                else getattr(getattr(parser, "config", None), "subtitle_offset", 0.0)
            ),
            "video": video,
            "subtitle": subtitle,
            "cue_indexes": cue_indexes,
        }
