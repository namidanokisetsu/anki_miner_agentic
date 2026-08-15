"""Atomic synchronization of explicitly mapped Anki knowledge sources."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict
from typing import Any, Protocol

from anki_miner.exceptions import AnkiConnectionError

from .analyzer import JapaneseAnalyzer, validate_analyzer_output
from .errors import AgentMiningError, require
from .models import AgentProfileConfig, KnowledgeSource, canonical_json, content_id
from .store import AgentStore


class ProfileAnkiGateway(Protocol):
    def get_deck_names(self) -> list[str]: ...

    def get_model_names(self) -> list[str]: ...

    def ordered_note_type_field_names(self, note_type: str) -> list[str]: ...

    def find_notes(self, query: str) -> list[int]: ...

    def notes_info(self, note_ids: list[int]) -> list[dict[str, Any]]: ...

    def find_cards(self, query: str) -> list[int]: ...

    def cards_info(self, card_ids: list[int]) -> list[dict[str, Any]]: ...


def _note_query(source: KnowledgeSource) -> str:
    note_type = source.note_type.replace("\\", "\\\\").replace('"', '\\"')
    deck = source.deck.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")
    return f'deck:"{deck}" note:"{note_type}"'


class LearnerProfileService:
    def __init__(
        self,
        store: AgentStore,
        analyzer: JapaneseAnalyzer,
        gateway: ProfileAnkiGateway,
        config: AgentProfileConfig,
    ) -> None:
        self.store = store
        self.analyzer = analyzer
        self.gateway = gateway
        self.config = config

    def validate_mapping(self) -> dict[str, Any]:
        validated: list[dict[str, Any]] = []
        fields_by_model: dict[str, list[str]] = {}
        available_decks = self.gateway.get_deck_names()
        configured_decks = {source.deck for source in self.config.knowledge_sources}
        configured_decks.add(self.config.write_target.deck)
        missing_decks = sorted(configured_decks - set(available_decks))
        require(
            not missing_decks,
            "deck_mapping_mismatch",
            "Configured Anki decks do not exist",
            missing=missing_decks,
            available_decks=available_decks,
        )
        available_models = self.gateway.get_model_names()
        configured_models = {source.note_type for source in self.config.knowledge_sources}
        configured_models.add(self.config.write_target.note_type)
        missing_models = sorted(configured_models - set(available_models))
        require(
            not missing_models,
            "note_type_mapping_mismatch",
            "Configured Anki note types do not exist",
            missing=missing_models,
            available_note_types=available_models,
        )
        for source in self.config.knowledge_sources:
            available = fields_by_model.setdefault(
                source.note_type,
                self.gateway.ordered_note_type_field_names(source.note_type),
            )
            selected = [*source.word_fields, *source.text_fields, *source.ignored_fields]
            missing = sorted(set(selected) - set(available))
            require(
                not missing,
                "field_mapping_mismatch",
                f"Mapped fields do not exist on note type {source.note_type}",
                note_type=source.note_type,
                missing=missing,
                available_fields=available,
            )
            validated.append(
                {
                    "deck": source.deck,
                    "note_type": source.note_type,
                    "word_fields": list(source.word_fields),
                    "text_fields": list(source.text_fields),
                }
            )
        enrichment_fields = {
            "chosen_definition": self.config.chosen_definition_field,
            "sentence_translation": self.config.sentence_translation_field,
        }
        mapped_enrichment_fields = {key: value for key, value in enrichment_fields.items() if value}
        if mapped_enrichment_fields:
            available = fields_by_model.setdefault(
                self.config.write_target.note_type,
                self.gateway.ordered_note_type_field_names(self.config.write_target.note_type),
            )
            missing = sorted(set(mapped_enrichment_fields.values()) - set(available))
            require(
                not missing,
                "field_mapping_mismatch",
                f"Mapped enrichment fields do not exist on note type {self.config.write_target.note_type}",
                note_type=self.config.write_target.note_type,
                missing=missing,
                available_fields=available,
            )
        return {
            "valid": True,
            "sources": validated,
            "write_target": asdict(self.config.write_target),
            "enrichment_fields": mapped_enrichment_fields,
        }

    def sync(self) -> dict[str, Any]:
        """Build the complete snapshot in memory, then publish it atomically."""
        self.validate_mapping()
        source_notes: dict[int, dict[str, Any]] = {}
        for source in self.config.knowledge_sources:
            note_ids = self.gateway.find_notes(_note_query(source))
            for start in range(0, len(note_ids), 1000):
                for raw_note in self.gateway.notes_info(note_ids[start : start + 1000]):
                    if not raw_note:
                        continue
                    note = self._normalize_note(raw_note, source)
                    note_id = note["note_id"]
                    previous = source_notes.get(note_id)
                    if previous is not None:
                        previous_roles = {(item["field_name"], item["role"]) for item in previous["fields"]}
                        next_roles = {(item["field_name"], item["role"]) for item in note["fields"]}
                        require(
                            previous_roles == next_roles,
                            "conflicting_field_roles",
                            "Overlapping knowledge sources assign different roles to the same note",
                            note_id=note_id,
                        )
                        continue
                    source_notes[note_id] = note

        cards: list[dict[str, Any]] = []
        suspended_only_note_ids: set[int] = set()
        cards_available = True
        card_ids = sorted(
            {
                int(card_id)
                for note in source_notes.values()
                for card_id in note.pop("card_ids", [])
                if isinstance(card_id, int)
            }
        )
        try:
            if not card_ids:
                for note_id in sorted(source_notes):
                    card_ids.extend(self.gateway.find_cards(f"nid:{note_id}"))
                card_ids = sorted(set(card_ids))
            for start in range(0, len(card_ids), 1000):
                cards.extend(
                    self._normalize_cards(self.gateway.cards_info(card_ids[start : start + 1000]), source_notes)
                )
        except (AnkiConnectionError, AgentMiningError) as exc:
            if "unsupported action" not in str(exc).lower():
                raise
            cards_available = False
            cards = []

        if cards_available:
            suspended_note_ids = {card["note_id"] for card in cards if card["queue"] == -1}
            active_note_ids = {card["note_id"] for card in cards if card["queue"] != -1}
            suspended_only_note_ids = suspended_note_ids - active_note_ids
            cards = [card for card in cards if card["queue"] != -1]

        lexical_state = self._aggregate(source_notes, cards, cards_available, suspended_only_note_ids)
        revision_material = {
            "analyzer": asdict(self.analyzer.identity),
            "config_hash": self.config.material_hash(),
            "notes": list(source_notes.values()),
            "cards": cards,
            "capabilities": {"cards_info": cards_available},
        }
        revision_id = content_id("profile", revision_material)
        snapshot = {
            **revision_material,
            "revision_id": revision_id,
            "analyzer_key": self.analyzer.identity.key,
            "lexical_state": lexical_state,
        }
        return self.store.publish_profile(snapshot)

    def _normalize_note(self, raw: dict[str, Any], source: KnowledgeSource) -> dict[str, Any]:
        note_id = raw.get("noteId", raw.get("note_id"))
        require(isinstance(note_id, int), "malformed_anki_response", "notesInfo row has no integer noteId")
        assert isinstance(note_id, int)
        model = raw.get("modelName", raw.get("model_name", source.note_type))
        require(
            model == source.note_type,
            "unexpected_note_type",
            "Anki returned a note outside the configured note type",
            note_id=note_id,
            expected=source.note_type,
            actual=model,
        )
        fields = raw.get("fields")
        require(isinstance(fields, dict), "malformed_anki_response", "notesInfo row has no field map", note_id=note_id)
        assert isinstance(fields, dict)
        normalized_fields: list[dict[str, Any]] = []
        for role, names in (("word", source.word_fields), ("text", source.text_fields)):
            for field_name in names:
                entry = fields.get(field_name)
                require(
                    isinstance(entry, dict) and isinstance(entry.get("value"), str),
                    "field_mapping_mismatch",
                    f"Mapped field {field_name} is absent from note type {source.note_type}",
                    note_id=note_id,
                    note_type=source.note_type,
                    available_fields=sorted(fields),
                )
                assert isinstance(entry, dict)
                assert isinstance(entry.get("value"), str)
                cleaned, tokens = validate_analyzer_output(self.analyzer, entry["value"])
                token_rows = [asdict(token) for token in tokens]
                normalized_fields.append(
                    {
                        "field_name": field_name,
                        "role": role,
                        "cleaned_text": cleaned,
                        "content_hash": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
                        "tokens": token_rows,
                    }
                )
        content_hash = hashlib.sha256(canonical_json(normalized_fields).encode("utf-8")).hexdigest()
        card_ids = raw.get("cards", [])
        require(
            isinstance(card_ids, list), "malformed_anki_response", "notesInfo cards must be a list", note_id=note_id
        )
        return {
            "note_id": note_id,
            "model_name": source.note_type,
            "deck_name": source.deck,
            "content_hash": content_hash,
            "fields": normalized_fields,
            "card_ids": card_ids,
        }

    def _normalize_cards(
        self, raw_cards: list[dict[str, Any]], notes: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize Anki card rows before applying learner-evidence policy."""
        result: list[dict[str, Any]] = []
        for raw in raw_cards:
            card_id = raw.get("cardId", raw.get("card_id"))
            note_id = raw.get("note", raw.get("noteId", raw.get("note_id")))
            require(isinstance(card_id, int), "malformed_anki_response", "cardsInfo row has no integer cardId")
            require(
                isinstance(note_id, int) and note_id in notes,
                "invalid_card_aggregate",
                "cardsInfo row is attached to an unknown note",
                card_id=card_id,
                note_id=note_id,
            )
            assert isinstance(note_id, int)
            interval = raw.get("interval", 0)
            reps = raw.get("reps", 0)
            lapses = raw.get("lapses", 0)
            require(
                all(isinstance(value, int) for value in (interval, reps, lapses)),
                "invalid_card_aggregate",
                "Card review aggregates must be integers",
                card_id=card_id,
            )
            require(
                interval >= 0 and reps >= 0 and 0 <= lapses <= reps,
                "invalid_card_aggregate",
                "Card review aggregates are impossible",
                card_id=card_id,
                interval=interval,
                reps=reps,
                lapses=lapses,
            )
            queue = raw.get("queue")
            result.append(
                {
                    "card_id": card_id,
                    "note_id": note_id,
                    "deck_name": str(raw.get("deckName", notes[note_id]["deck_name"])),
                    "interval_days": interval,
                    "reps": reps,
                    "lapses": lapses,
                    "queue": queue,
                    "card_type": raw.get("type"),
                }
            )
        return result

    def _aggregate(
        self,
        notes: dict[int, dict[str, Any]],
        cards: list[dict[str, Any]],
        cards_available: bool,
        suspended_only_note_ids: set[int],
    ) -> list[dict[str, Any]]:
        word_fields: dict[str, set[tuple[int, str]]] = defaultdict(set)
        text_fields: dict[str, set[tuple[int, str]]] = defaultdict(set)
        word_lexemes_by_note: dict[int, set[str]] = defaultdict(set)
        for note in notes.values():
            # A suspended card is an explicit opt-out from knowledge evidence.
            # If the note has another active card, that card still supplies the
            # note's target-field evidence.
            if note["note_id"] in suspended_only_note_ids:
                continue
            for field in note["fields"]:
                lexemes = {token["lexical_id"] for token in field["tokens"] if token["lexical_id"]}
                target = word_fields if field["role"] == "word" else text_fields
                for lexical_id in lexemes:
                    target[lexical_id].add((note["note_id"], field["field_name"]))
                if field["role"] == "word":
                    word_lexemes_by_note[note["note_id"]].update(lexemes)
        cards_by_lexeme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for card in cards:
            for lexical_id in word_lexemes_by_note[card["note_id"]]:
                cards_by_lexeme[lexical_id].append(card)
        rows: list[dict[str, Any]] = []
        for lexical_id in sorted(set(word_fields) | set(text_fields)):
            evidence = cards_by_lexeme[lexical_id]
            interval = max((card["interval_days"] for card in evidence), default=None)
            reps = sum(card["reps"] for card in evidence)
            lapses = sum(card["lapses"] for card in evidence)
            if not cards_available or not evidence:
                state = "unseen" if not evidence else "learning"
            elif interval is not None and interval >= self.config.mature_interval_days:
                state = "mature"
            elif reps > 0:
                state = "young"
            else:
                state = "learning"
            rows.append(
                {
                    "lexical_id": lexical_id,
                    "word_exposures": len(word_fields[lexical_id]),
                    "sentence_exposures": len(text_fields[lexical_id]),
                    "card_count": len(evidence),
                    "reps": reps,
                    "lapses": lapses,
                    "interval_days": interval,
                    "state": state,
                }
            )
        return rows
