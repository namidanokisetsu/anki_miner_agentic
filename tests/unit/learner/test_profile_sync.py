from __future__ import annotations

import pytest

from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig, AnalysisToken, AnalyzerIdentity, KnowledgeSource, WriteTarget
from anki_miner.agent.profile import LearnerProfileService
from anki_miner.agent.store import AgentStore


class FakeAnalyzer:
    identity = AnalyzerIdentity(1, "fake", "fixture")

    def analyze(self, text: str | bytes):
        assert isinstance(text, str)
        if not text:
            return []
        surface = text.split()[0]
        start = text.index(surface)
        return [AnalysisToken(surface, surface, surface, surface, "名詞", "一般", start, start + len(surface))]


class FakeGateway:
    def __init__(self) -> None:
        self.bad_cards = False

    def ordered_note_type_field_names(self, model_name: str):
        return ["word", "sentence", "answer"]

    def find_notes(self, query: str):
        return [1]

    def notes_info(self, note_ids: list[int]):
        return [
            {
                "noteId": 1,
                "modelName": "ExampleNote",
                "fields": {
                    "word": {"value": "<b>食べる</b> [sound:x.mp3]"},
                    "sentence": {"value": "食べる&nbsp;。"},
                    "answer": {"value": "eat"},
                },
                "cards": [10],
            }
        ]

    def find_cards(self, query: str):
        return [10]

    def cards_info(self, card_ids: list[int]):
        lapses = 4 if self.bad_cards else 1
        return [
            {
                "cardId": 10,
                "note": 1,
                "deckName": "Deck A",
                "interval": 30,
                "reps": 3,
                "lapses": lapses,
                "queue": 2,
                "type": 2,
            }
        ]


def config() -> AgentProfileConfig:
    return AgentProfileConfig(
        knowledge_sources=(KnowledgeSource("Deck A", "ExampleNote", ("word",), ("sentence",), ("answer",)),),
        write_target=WriteTarget("Destination", "ExampleNote"),
    )


def test_sync_cleans_aggregates_and_is_idempotent(tmp_path):
    store = AgentStore(tmp_path / "learner.sqlite3")
    service = LearnerProfileService(store, FakeAnalyzer(), FakeGateway(), config())

    first = service.sync()
    second = service.sync()

    assert first["revision_id"] == second["revision_id"]
    assert first["note_count"] == 1
    state = store.lexical_features()["食べる"]
    assert state == {
        "state": "mature",
        "word_exposures": 1,
        "sentence_exposures": 1,
        "word_card_count": 1,
        "reviews": 3,
        "lapses": 1,
        "interval_days": 30,
    }


def test_bad_refresh_does_not_replace_published_profile(tmp_path):
    store = AgentStore(tmp_path / "learner.sqlite3")
    gateway = FakeGateway()
    service = LearnerProfileService(store, FakeAnalyzer(), gateway, config())
    valid = service.sync()
    gateway.bad_cards = True

    with pytest.raises(AgentMiningError, match="impossible"):
        service.sync()

    assert store.profile_status()["revision_id"] == valid["revision_id"]


def test_mapping_error_names_model_and_available_fields(tmp_path):
    cfg = AgentProfileConfig(
        (KnowledgeSource("Deck A", "ExampleNote", ("renamed",), ()),),
        WriteTarget("Destination", "ExampleNote"),
    )
    service = LearnerProfileService(AgentStore(tmp_path / "db.sqlite3"), FakeAnalyzer(), FakeGateway(), cfg)
    with pytest.raises(AgentMiningError) as raised:
        service.validate_mapping()
    assert raised.value.code == "field_mapping_mismatch"
    assert raised.value.details == {
        "note_type": "ExampleNote",
        "missing": ["renamed"],
        "available_fields": ["word", "sentence", "answer"],
    }
