"""Opt-in contract tests against a real, separate upstream checkout.

Anki calls are stubbed; every file and dictionary lives in a temporary home.
The parser, service factory, filtering pipeline and API selection are real.
"""

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import candidates


@unittest.skipUnless(os.environ.get("ANKI_MINER_TEST_SOURCE"), "Set ANKI_MINER_TEST_SOURCE to test upstream")
class UpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.home.cleanup)
        env = patch.dict(os.environ, {"ANKI_MINER_HOME": cls.home.name, "QT_QPA_PLATFORM": "offscreen"})
        env.start()
        cls.addClassCleanup(env.stop)
        source = str(Path(os.environ["ANKI_MINER_TEST_SOURCE"]).resolve())
        sys.path.insert(0, source)
        cls.addClassCleanup(lambda: sys.path.remove(source))
        from anki_miner.config import AnkiMinerConfig, ChainEntry
        from anki_miner.services.dictionary.storage import (
            SCHEMA_VERSION,
            DictRow,
            bulk_insert,
            create_index,
            write_meta,
        )

        cls.root = Path(cls.home.name)
        database = cls.root / "dicts" / "fixture" / "index.sqlite"
        database.parent.mkdir(parents=True)
        create_index(database)
        words = {
            "学校": "がっこう",
            "勉強": "べんきょう",
            "本": "ほん",
            "買う": "かう",
            "人工知能": "じんこうちのう",
            "人工": "じんこう",
            "知能": "ちのう",
        }
        bulk_insert(
            database,
            [
                DictRow(term=w, reading=r, content='<li class="gloss-item">test</li>', sequence=i)
                for i, (w, r) in enumerate(words.items(), 1)
            ],
        )
        write_meta(
            database,
            {
                "schema_version": str(SCHEMA_VERSION),
                "source_name": "fixture",
                "format": "yomitan",
                "entry_count": str(len(words)),
            },
        )
        base = AnkiMinerConfig()
        fields = {key: "" for key in base.anki_fields}
        fields.update(word="Front", definition="Back")
        cls.config = replace(
            base,
            anki_deck_name="Test",
            anki_note_type="Basic",
            anki_fields=fields,
            ankiconnect_url="http://127.0.0.1:1",
            dicts_root=database.parent.parent,
            dictionary_chain=(ChainEntry(kind="indexed", dict_id="fixture", enabled=True),),
            known_words_db_path=cls.root / "known.db",
            stats_db_path=cls.root / "stats.db",
            media_temp_folder=cls.root / "media",
            include_known_words=False,
            use_known_words_db=False,
            deduplicate_sentences=False,
            use_i_plus_one_filter=False,
            merge_incomplete_cues=False,
            min_frequency_rank=0,
            max_frequency_rank=0,
            excluded_wordsets=[],
            use_blacklist=False,
            use_whitelist=False,
        )

    def setUp(self):
        from anki_miner.services.anki_service import AnkiService

        self.addCleanup(patch.stopall)
        patch.object(AnkiService, "verify_card_target").start()
        self.known = patch.object(AnkiService, "get_existing_vocabulary", return_value=set()).start()
        self.writer = patch.object(
            AnkiService, "create_cards_batch", side_effect=AssertionError("No writes")
        ).start()
        patch(
            "requests.sessions.Session.request", side_effect=AssertionError("Live network forbidden")
        ).start()
        self.subtitles = self.root / "episode.srt"
        self.source = {
            "subtitle_file": str(self.subtitles),
            "video_file": str(self.root / "video.mp4"),
            "subtitle_offset": 0.0,
        }
        self.write_cues("学校で勉強する。", "本を買う。")

    def write_cues(self, *cues):
        self.subtitles.write_text(
            "\n".join(
                f"{i}\n00:00:{i * 3:02},000 --> 00:00:{i * 3 + 2:02},000\n{text}\n"
                for i, text in enumerate(cues, 1)
            ),
            encoding="utf-8",
        )

    def discover(self, **changes):
        result = candidates.discover(replace(self.config, **changes), [self.source], 100)
        self.writer.assert_not_called()
        return result["candidates"]

    def test_real_parser_dictionary_and_known_word_filter(self):
        self.known.return_value = {"学校"}
        rows = self.discover()
        self.assertEqual({row["word"] for row in rows}, {"勉強", "本", "買う"})
        self.assertIn("学校", {row["word"] for row in self.discover(include_known_words=True)})
        self.assertFalse((self.root / "stats.db").exists())

    def test_i_plus_one_and_blacklist_are_upstream_settings(self):
        self.known.return_value = {"学校"}
        rows = self.discover(use_i_plus_one_filter=True)
        self.assertEqual([row["word"] for row in rows], ["勉強"])
        blacklist = self.root / "blacklist.txt"
        blacklist.write_text("勉強\n", encoding="utf-8")
        self.assertNotIn(
            "勉強", {row["word"] for row in self.discover(use_blacklist=True, blacklist_path=blacklist)}
        )

    def test_compounds_use_upstream_dictionary(self):
        self.write_cues("人工知能。")
        self.assertEqual([row["word"] for row in self.discover()], ["人工知能"])

    def test_i_plus_one_picks_a_different_sentence(self):
        self.write_cues("学校で勉強する。", "勉強する。")
        rows = self.discover(use_i_plus_one_filter=True)
        self.assertEqual([row["word"] for row in rows], ["勉強"])
        self.assertEqual(rows[0]["sentence"], "勉強する。")
        self.assertEqual(rows[0]["line_start"], 6.0)

    def test_offset_and_merge_round_trip_through_public_api_selection(self):
        from anki_miner.cli.api.files import WordRequest
        from anki_miner.cli.api.lines import WordSelection, line_merges
        from anki_miner.gui.utils.service_factory import create_episode_processor
        from anki_miner.presenters.null_presenter import NullPresenter
        from anki_miner.services.cue_merge import merge_budget_seconds

        self.write_cues("学校で", "勉強する。")
        self.source["subtitle_offset"] = -4.0
        config = replace(self.config, merge_incomplete_cues=True)
        rows = candidates.discover(config, [self.source], 100)["candidates"]
        self.assertTrue(any(row["line_expansion"] != [0, 0] for row in rows))
        processor = create_episode_processor(config, NullPresenter())
        try:
            path = self.subtitles
            entries = processor.subtitle_parser.parse_raw_entries(path, -4.0)
            raw = processor.subtitle_parser.parse_raw_entries(path, 0.0)
            words = processor.subtitle_parser.parse_subtitle_file(path, subtitle_offset=-4.0)
            _, lines = processor.subtitle_parser.parse_subtitle_file_with_index(path, subtitle_offset=-4.0)
            processor.word_filter.attach_sentence_candidates(words, lines)
            requests = [
                WordRequest(
                    word=row["word"],
                    line_start=row["line_start"],
                    line_expansion=tuple(row["line_expansion"]),
                )
                for row in rows
            ]
            selection = WordSelection(
                requests,
                entries,
                raw,
                line_merges(config, entries),
                merge_budget_seconds(config.audio_padding),
            )
            selected = selection(words)
            materialized = processor._materialize_line_expansions(selected, path, -4.0)
            self.assertEqual([word.sentence for word in materialized], [row["sentence"] for row in rows])
            self.assertEqual([row["line_start"] for row in rows], [3.0, 6.0])
        finally:
            processor.close()

    def test_bounded_pool_and_empty_filter_result(self):
        result = candidates.discover(self.config, [self.source, self.source], 1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["total_candidates"], 4)
        self.known.return_value = {"学校", "勉強", "本", "買う"}
        self.assertEqual(self.discover(), [])

    def test_profile_resolution_keeps_known_word_and_i_plus_one_settings(self):
        from anki_miner.gui.utils.config_manager import GUIConfigManager

        GUIConfigManager.save_config(replace(self.config, use_i_plus_one_filter=True))
        config, state = candidates.load_settings({"profile": None, "language": "ja"})
        self.assertFalse(config.include_known_words)
        self.assertTrue(config.use_i_plus_one_filter)
        self.assertFalse(config.allow_duplicate_cards)
        self.assertTrue(state["settings_hash"])

    def test_bridge_entry_uses_saved_profile_and_returns_json_serializable_results(self):
        import json

        from anki_miner.gui.utils.config_manager import GUIConfigManager

        self.known.return_value = {"学校"}
        GUIConfigManager.save_config(replace(self.config, use_i_plus_one_filter=True))
        request = {
            "profile": None,
            "language": "ja",
            "command": "discover",
            "sources": [self.source],
            "limit": 3,
        }
        result = candidates.execute(request)
        self.assertEqual([row["word"] for row in result["candidates"]], ["勉強"])
        self.assertEqual(result["target"]["anki_fields"]["word"], "Front")
        self.assertTrue(json.dumps(result))
        state = candidates.execute({**request, "command": "inspect"})
        self.assertEqual(state["upstream_state"], result["upstream_state"])


if __name__ == "__main__":
    unittest.main()
