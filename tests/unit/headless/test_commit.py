from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from anki_miner.agent.candidates import file_fingerprint
from anki_miner.agent.commit import ExistingPipelineCandidateWriter, MiningCommitService
from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig, KnowledgeSource, WriteTarget
from anki_miner.agent.store import AgentStore
from anki_miner.models import MediaData, TokenizedWord


class Writer:
    def __init__(self):
        self.calls = 0
        self.candidates = []

    def create(self, candidate):
        self.calls += 1
        self.candidates.append(candidate)
        return {"outcome": "created", "note_id": 100 + self.calls, "media": {}}


def cfg(enabled=True, **kwargs):
    return AgentProfileConfig(
        (KnowledgeSource("Deck", "ExampleNote", ("word",), ("sentence",)),),
        WriteTarget("Destination", "ExampleNote", enabled=enabled),
        **kwargs,
    )


def make_batch(store, tmp_path, eligible=True):
    video = tmp_path / "v.mp4"
    subtitle = tmp_path / "s.srt"
    video.write_bytes(b"v")
    subtitle.write_text("s", encoding="utf-8")
    public = {
        "candidate_id": "candidate_one",
        "eligible": eligible,
        "definition_options": [{"dictionary": "Dictionary", "text": "to eat, consume"}],
    }
    internal = {
        "video_fingerprint": file_fingerprint(video),
        "subtitle_fingerprint": file_fingerprint(subtitle),
    }
    store.create_batch(
        {
            "revision_id": "batch_one",
            "profile_revision_id": "profile_one",
            "analyzer_key": "a",
            "config_hash": "c",
            "request_hash": "r",
            "sources": [],
            "max_cards": 2,
        },
        [{"lexical_id": "食べる", "public": public, "internal": internal}],
    )


def test_commit_is_selected_only_and_idempotent(tmp_path):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path)
    writer = Writer()
    service = MiningCommitService(store, cfg(), writer)

    dry_run = service.commit("batch_one", ["candidate_one"], dry_run=True)
    first = service.commit("batch_one", ["candidate_one"], dry_run=False, validation_token=dry_run["validation_token"])
    second = service.commit("batch_one", ["candidate_one"], dry_run=False, validation_token=dry_run["validation_token"])

    assert first["state"] == "completed"
    assert second["job_id"] == first["job_id"]
    assert writer.calls == 1
    assert first["outputs"][0]["note_id"] == 101


def test_dry_run_never_writes_and_ineligible_cannot_be_selected(tmp_path):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path, eligible=False)
    writer = Writer()
    service = MiningCommitService(store, cfg(), writer)
    with pytest.raises(AgentMiningError) as raised:
        service.commit("batch_one", ["candidate_one"], dry_run=True)
    assert raised.value.code == "ineligible_selection"
    assert writer.calls == 0


def test_commit_passes_bounded_enrichments_to_writer(tmp_path):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path)
    writer = Writer()
    service = MiningCommitService(
        store,
        cfg(chosen_definition_field="Chosen", sentence_translation_field="Translation"),
        writer,
    )
    enrichments = {
        "candidate_one": {
            "chosen_definition": "to eat, consume",
            "sentence_translation": "I ate sushi.",
        }
    }

    dry_run = service.commit("batch_one", ["candidate_one"], enrichments=enrichments, dry_run=True)
    result = service.commit(
        "batch_one",
        ["candidate_one"],
        enrichments=enrichments,
        dry_run=False,
        validation_token=dry_run["validation_token"],
    )

    assert dry_run["enriched_count"] == 1
    assert result["selection"]["enrichments"] == enrichments
    assert writer.candidates[0]["enrichment"] == enrichments["candidate_one"]


def test_chosen_definition_may_faithfully_paraphrase_dictionary_text(tmp_path):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path)
    writer = Writer()
    service = MiningCommitService(store, cfg(chosen_definition_field="Chosen"), writer)
    enrichments = {"candidate_one": {"chosen_definition": "to have a meal"}}

    dry_run = service.commit("batch_one", ["candidate_one"], enrichments=enrichments, dry_run=True)
    service.commit(
        "batch_one",
        ["candidate_one"],
        enrichments=enrichments,
        dry_run=False,
        validation_token=dry_run["validation_token"],
    )

    assert writer.candidates[0]["enrichment"] == enrichments["candidate_one"]


def test_live_commit_requires_matching_dry_run_token(tmp_path):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path)
    writer = Writer()
    service = MiningCommitService(store, cfg(), writer)

    with pytest.raises(AgentMiningError) as missing:
        service.commit("batch_one", ["candidate_one"], dry_run=False)
    assert missing.value.code == "dry_run_required"

    dry_run = service.commit("batch_one", ["candidate_one"], dry_run=True)
    with pytest.raises(AgentMiningError) as changed:
        service.commit(
            "batch_one",
            ["candidate_one"],
            metadata={"candidate_one": {"score": 0.5}},
            dry_run=False,
            validation_token=dry_run["validation_token"],
        )
    assert changed.value.code == "validated_selection_changed"
    assert writer.calls == 0


@pytest.mark.parametrize(
    ("enrichment", "code"),
    [
        ({"sentence_translation": "line one\nline two"}, "invalid_enrichment"),
        ({"chosen_definition": "to eat"}, "unmapped_enrichment"),
    ],
)
def test_enrichment_rejects_multiline_and_unmapped_values(tmp_path, enrichment, code):
    store = AgentStore(tmp_path / "db.sqlite3")
    make_batch(store, tmp_path)
    service = MiningCommitService(store, cfg(sentence_translation_field="Translation"), Writer())

    with pytest.raises(AgentMiningError) as raised:
        service.commit(
            "batch_one",
            ["candidate_one"],
            enrichments={"candidate_one": enrichment},
            dry_run=True,
        )

    assert raised.value.code == code


def test_existing_pipeline_writer_forwards_enrichments_to_card_builder(tmp_path):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    word = TokenizedWord("食べた", "食べる", "タベタ", "寿司を食べた。", 1.0, 2.0, 1.0)
    processor = MagicMock()
    processor._phase3_extract.return_value = [(word, MediaData())]
    processor._phase4_lookup.return_value = (["full definition"], [None], [(None, None)])
    processor._phase5_create.return_value = (1, [123], ["食べる"])
    processor.anki_service = SimpleNamespace(last_skipped_duplicates=0)
    writer = ExistingPipelineCandidateWriter(processor)
    enrichment = {"chosen_definition": "to eat, consume", "sentence_translation": "I ate sushi."}

    result = writer.create(
        {
            "internal": {
                "word": {**asdict(word), "mined_form": word.mined_form},
                "video_fingerprint": {"path": str(video)},
                "subtitle_fingerprint": {"path": str(subtitle)},
                "episode_id": "episode",
                "audio_track": "japanese",
            },
            "enrichment": enrichment,
        }
    )

    assert result["outcome"] == "created"
    assert processor._phase5_create.call_args.kwargs["card_extra_fields"] == [enrichment]
