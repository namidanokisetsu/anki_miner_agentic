from dataclasses import replace

from anki_miner.config import AnkiMinerConfig
from anki_miner.models import CardPayload, MediaData, TokenizedWord
from anki_miner.services.anki_note_builder import build_note


def test_note_builder_writes_enrichments_as_escaped_plain_text():
    base = AnkiMinerConfig()
    config = replace(
        base,
        anki_fields={
            **dict(base.anki_fields),
            "chosen_definition": "Chosen",
            "sentence_translation": "Translation",
        },
    )
    word = TokenizedWord("食べた", "食べる", "タベタ", "寿司を食べた。", 1.0, 2.0, 1.0)
    payload = CardPayload(
        word,
        MediaData(),
        "full definition",
        {"chosen_definition": "to eat, consume", "sentence_translation": "I ate <sushi>."},
    )

    fields = build_note(payload, config, set()).note["fields"]

    assert fields["Chosen"] == "to eat, consume"
    assert fields["Translation"] == "I ate &lt;sushi&gt;."
