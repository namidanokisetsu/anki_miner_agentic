from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

from anki_miner.agent.candidates import CandidateBatchService
from anki_miner.agent.models import (
    AgentProfileConfig,
    AnalyzerIdentity,
    KnowledgeSource,
    LocalEpisodeInput,
    WriteTarget,
    YouTubeInput,
)
from anki_miner.agent.store import AgentStore
from anki_miner.config import AnkiMinerConfig
from anki_miner.exceptions.youtube import NoJapaneseSubtitlesError
from anki_miner.models import TokenizedWord


class Analyzer:
    identity = AnalyzerIdentity(1, "fake", "fixture")


class Parser:
    def __init__(self, subtitle_offset=0.0):
        self.config = SimpleNamespace(subtitle_offset=subtitle_offset)

    def with_subtitle_offset(self, subtitle_offset):
        return type(self)(subtitle_offset)

    def parse_subtitle_file_with_index(self, path):
        start = max(0.0, 1.0 + self.config.subtitle_offset)
        end = max(start, 2.5 + self.config.subtitle_offset)
        return (
            [
                TokenizedWord(
                    surface="食べた",
                    lemma="食べる",
                    reading="タベタ",
                    sentence="寿司を食べた。",
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    orth_base="食べる",
                    expression_reading="たべる",
                    pos="動詞",
                    surface_start=3,
                    surface_end=6,
                )
            ],
            [],
        )

    def count_lemmas(self, path):
        return Counter({"食べる": 2})

    def parse_raw_entries(self, path):
        start = max(0.0, 1.0 + self.config.subtitle_offset)
        end = max(start, 2.5 + self.config.subtitle_offset)
        return [(start, end, "寿司を食べた。")]


class Filter:
    def attach_occurrence_counts(self, words, counts):
        for word in words:
            word.occurrence_count = counts[word.lemma]

    def attach_sentence_candidates(self, words, line_index, max_candidates):
        return None


def cfg(**kwargs):
    return AgentProfileConfig(
        (KnowledgeSource("Deck", "ExampleNote", ("word",), ("sentence",)),),
        WriteTarget("Destination", "ExampleNote"),
        **kwargs,
    )


def publish_empty_profile(store):
    store.publish_profile(
        {
            "revision_id": "profile_fixture",
            "analyzer_key": Analyzer.identity.key,
            "analyzer": {"contract_version": 1, "backend": "fake", "dictionary": "fixture"},
            "config_hash": cfg().material_hash(),
            "capabilities": {"cards_info": True},
            "notes": [],
            "cards": [],
            "lexical_state": [],
        }
    )


def test_prepare_is_persistent_compact_and_idempotent(tmp_path):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    first = service.prepare([LocalEpisodeInput(video, subtitle)])
    second = service.prepare([LocalEpisodeInput(video, subtitle)])
    page = store.list_candidates(
        first["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=False,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )

    assert second["batch_revision"] == first["batch_revision"]
    assert page["total"] == 1
    candidate = page["candidates"][0]
    assert candidate["target"]["mined_form"] == "食べる"
    assert candidate["signals"]["episode_occurrences"] == 2
    assert candidate["episode"]["subtitle_source"] == "local"
    assert candidate["episode"]["audio_track"] == "japanese"
    assert "video_fingerprint" not in candidate


def test_prepare_applies_per_input_subtitle_offset(tmp_path):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    batch = service.prepare([LocalEpisodeInput(video, subtitle, subtitle_offset=2.25)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=False,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )

    candidate = page["candidates"][0]
    assert candidate["episode"]["start_ms"] == 3250
    assert candidate["episode"]["end_ms"] == 4750
    assert candidate["episode"]["subtitle_offset"] == 2.25


def test_prepare_rebuilds_after_dictionary_availability_changes(tmp_path):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    dictionary_ready = False

    def probe(terms):
        return dict.fromkeys(terms, dictionary_ready)

    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(),
        definition_probe=probe,
    )

    before = service.prepare([LocalEpisodeInput(video, subtitle)])
    dictionary_ready = True
    after = service.prepare([LocalEpisodeInput(video, subtitle)])

    assert before["batch_revision"] != after["batch_revision"]
    assert before["eligible_count"] == 0
    assert after["eligible_count"] == 1


def test_prepare_exposes_bounded_plain_text_definition_options(tmp_path):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(
            chosen_definition_field="Chosen",
            sentence_translation_field="Translation",
            max_definition_option_chars=100,
        ),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
        definition_options_lookup=lambda term: [
            (" First Dictionary ", "<div><b>to eat</b>, consume<script>ignore me</script></div>"),
            ("Second Dictionary", "<ol><li>to live on</li></ol>"),
        ],
    )

    batch = service.prepare([LocalEpisodeInput(video, subtitle)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=False,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )

    assert page["candidates"][0]["definition_options"] == [
        {"option_id": "definition_1", "dictionary": "First Dictionary", "text": "to eat, consume"},
        {"option_id": "definition_2", "dictionary": "Second Dictionary", "text": "to live on"},
    ]
    assert page["candidates"][0]["allowed_enrichments"] == ["chosen_definition", "sentence_translation"]


def test_prepare_persists_frequency_sort_rank_for_commit(tmp_path):
    class FrequencyService:
        def is_available(self):
            return True

        def lookup_all_many(self, pairs):
            return [[("Frequency Source", 2494, "2494")]] * len(pairs)

    class PitchService:
        def is_available(self):
            return True

        def lookup_batch_detailed(self, keys, *, fmt):
            assert fmt == "jp"
            return [("3", "中高")] * len(keys)

    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
        frequency_service=FrequencyService(),
        pitch_service=PitchService(),
    )

    batch = service.prepare([LocalEpisodeInput(video, subtitle)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=1,
        include_ineligible=False,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )
    candidate_id = page["candidates"][0]["candidate_id"]
    candidate = store.get_candidates(batch["batch_revision"], [candidate_id])[0]

    assert candidate["public"]["signals"]["frequency_rank"] == 2494
    assert candidate["public"]["signals"]["pitch_available"] is True
    assert candidate["public"]["pitch"] == {"position": "3", "category": "中高"}
    assert candidate["internal"]["word"]["frequency_harmonic_rank"] == 2494


def test_captionless_youtube_can_opt_into_local_asr(tmp_path):
    class Info:
        video_id = "abc123def45"
        has_manual_ja_subs = False
        has_auto_ja_subs = False

    class Fetcher:
        def probe_metadata(self, url):
            return Info()

        def fetch_video(self, url, video_id, workspace, mode, **kwargs):
            (workspace / f"{video_id}.mp4").write_bytes(b"video")
            raise NoJapaneseSubtitlesError("no captions")

    def generate_asr(video_file, subtitle_file, audio_track_override):
        assert video_file.name == "abc123def45.mp4"
        assert audio_track_override is None
        subtitle_file.write_text("fixture", encoding="utf-8")
        return subtitle_file

    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        Parser(),
        Filter(),
        cfg(),
        youtube_fetcher=Fetcher(),
        asr_generator=generate_asr,
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    batch = service.prepare([YouTubeInput("https://youtu.be/abc123def45", allow_asr=True)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=False,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )

    candidate = page["candidates"][0]
    assert candidate["episode"]["subtitle_source"] == "local_asr"
    assert candidate["episode"]["audio_track"] == "japanese"


def test_prepare_uses_one_reusable_subtitle_parse(tmp_path):
    class UnifiedParser(Parser):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def parse_mining_episode(self, path):
            self.calls += 1
            words, line_index = super().parse_subtitle_file_with_index(path)
            return words, line_index, super().count_lemmas(path), super().parse_raw_entries(path)

        def parse_subtitle_file_with_index(self, path):  # pragma: no cover - must not be called directly
            raise AssertionError("legacy parse path used")

        def count_lemmas(self, path):  # pragma: no cover - must not be called directly
            raise AssertionError("legacy count path used")

        def parse_raw_entries(self, path):  # pragma: no cover - must not be called directly
            raise AssertionError("legacy raw-entry path used")

    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    parser = UnifiedParser()
    service = CandidateBatchService(
        store,
        Analyzer(),
        parser,
        Filter(),
        cfg(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    service.prepare([LocalEpisodeInput(video, subtitle)])

    assert parser.calls == 1


def test_definition_options_load_only_after_eligibility_and_shortlist_bounds(tmp_path):
    class TwoWordParser(Parser):
        def parse_subtitle_file_with_index(self, path):
            first, index = super().parse_subtitle_file_with_index(path)
            excluded = TokenizedWord(
                surface="除外",
                lemma="除外",
                reading="ジョガイ",
                sentence="除外する。",
                start_time=3.0,
                end_time=4.0,
                duration=1.0,
                orth_base="除外",
                expression_reading="じょがい",
                pos="名詞",
            )
            return [*first, excluded], index

        def count_lemmas(self, path):
            return Counter({"食べる": 2, "除外": 1})

        def parse_raw_entries(self, path):
            return [*super().parse_raw_entries(path), (3.0, 4.0, "除外する。")]

    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    calls = []

    class WordLists:
        def is_available(self):
            return True

        def is_whitelisted(self, word):
            return False

        def is_blacklisted(self, word):
            return word == "除外"

    service = CandidateBatchService(
        store,
        Analyzer(),
        TwoWordParser(),
        Filter(),
        cfg(chosen_definition_field="Chosen", max_cards=1),
        word_list_service=WordLists(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
        definition_options_lookup=lambda term: calls.append(term) or [("D", "meaning")],
    )

    service.prepare([LocalEpisodeInput(video, subtitle)])

    assert calls == ["食べる"]


def _word(surface, sentence, start, *, reading="ヨミ", pos="名詞"):
    return TokenizedWord(
        surface=surface,
        lemma=surface,
        reading=reading,
        sentence=sentence,
        start_time=start,
        end_time=start + 2.0,
        duration=2.0,
        orth_base=surface,
        expression_reading=reading,
        pos=pos,
    )


def test_conservative_guard_exposes_exact_candidate_unknown_items(tmp_path):
    sentence = "文明は未来を脅かす。"

    class ThreeWordParser(Parser):
        def parse_subtitle_file_with_index(self, path):
            return [
                _word("文明", sentence, 1.0),
                _word("未来", sentence, 1.0),
                _word("脅かす", sentence, 1.0, pos="動詞"),
            ], []

        def count_lemmas(self, path):
            return Counter({"文明": 1, "未来": 1, "脅かす": 1})

        def parse_raw_entries(self, path):
            return [(1.0, 3.0, sentence)]

    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        ThreeWordParser(),
        Filter(),
        cfg(),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    batch = service.prepare([LocalEpisodeInput(video, subtitle)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=True,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )

    assert len(page["candidates"]) == 3
    for candidate in page["candidates"]:
        assert candidate["sentence"]["candidate_unknown_items"] == ["文明", "未来", "脅かす"]
        assert candidate["difficulty"]["calibrated_score"] is None
        assert "too_many_candidate_unknowns" in candidate["eligibility"]["reason_codes"]


def test_target_competition_precedes_sentence_deduplication(tmp_path):
    sentence = "ゴールが文明を脅かす。"

    class CompetingParser(Parser):
        def parse_subtitle_file_with_index(self, path):
            return [
                _word("ゴール", sentence, 1.0, reading="ゴール"),
                _word("脅かす", sentence, 1.0, pos="動詞"),
            ], []

        def count_lemmas(self, path):
            return Counter({"ゴール": 3, "脅かす": 1})

        def parse_raw_entries(self, path):
            return [(1.0, 3.0, sentence)]

    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    store = AgentStore(tmp_path / "agent.sqlite3")
    publish_empty_profile(store)
    service = CandidateBatchService(
        store,
        Analyzer(),
        CompetingParser(),
        Filter(),
        cfg(),
        mining_policy=replace(AnkiMinerConfig(), deduplicate_sentences=True),
        definition_probe=lambda terms: dict.fromkeys(terms, True),
    )

    batch = service.prepare([LocalEpisodeInput(video, subtitle)])
    page = store.list_candidates(
        batch["batch_revision"],
        offset=0,
        limit=10,
        include_ineligible=True,
        expected_schema_version=1,
        max_payload_bytes=100_000,
    )
    by_target = {item["target"]["mined_form"]: item for item in page["candidates"]}

    assert by_target["脅かす"]["eligible"] is True
    assert by_target["ゴール"]["eligible"] is False
    assert "weaker_sentence_target" in by_target["ゴール"]["eligibility"]["reason_codes"]
