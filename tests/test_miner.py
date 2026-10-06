import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import miner
import transport


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / "episode.mp4"
        self.subtitles = self.root / "episode.srt"
        self.video.write_bytes(b"test video")
        self.subtitles.write_text("1\n00:00:01,000 --> 00:00:03,000\n日本語を勉強する。\n", encoding="utf-8")
        self.config = {
            "upstream_python": "mock-python",
            "profile": None,
            "language": "ja",
        }
        self.request = {
            "max_cards": 1,
            "inputs": [{"video_file": str(self.video), "subtitle_file": str(self.subtitles)}],
        }
        self.folder = self.root / "run"
        self.calls = []
        self.statuses = ["created"]
        self.failure = None
        self.api = patch("miner.upstream", side_effect=self.fake_upstream).start()
        self.addCleanup(patch.stopall)
        self.prepared = {
            "target": {
                "anki_deck_name": "Japanese",
                "anki_note_type": "Vocabulary",
                "anki_fields": {"word": "Expression"},
                "allow_duplicate_cards": False,
            },
            "upstream_state": {"version": "3.6.0", "settings_hash": "fixture"},
            "total_candidates": 2,
            "candidates": [
                {
                    "id": "c0001",
                    "word": "study",
                    "sentence": "test sentence",
                    "source": 0,
                    "line_start": 1.0,
                    "line_expansion": [0, 1],
                    "occurrences": 2,
                },
                {
                    "id": "c0002",
                    "word": "Japanese",
                    "sentence": "test sentence",
                    "source": 0,
                    "line_start": 1.0,
                    "line_expansion": [0, 0],
                    "occurrences": 1,
                },
            ],
        }
        self.bridge = patch("miner.bridge", side_effect=lambda *a, **kw: copy.deepcopy(self.prepared)).start()

    def fake_upstream(self, config, command, *args, **kwargs):
        if command == "check":
            return {"schema": 1, "ok": True, "result": {"ready": True, "items": []}}
        if command == "version":
            return {"schema": 1, "ok": True, "result": {"schema": 1, "app": "3.6.0", "commands": ["mine"]}}
        self.assertEqual(command, "mine")
        job = transport.read_json(args[0])
        self.calls.append(job)
        if self.failure:
            raise self.failure
        return self.write_results(job)

    def write_results(self, job):
        runs = []
        for episode in job["episodes"]:
            directory = Path(job["run_dir"]) / episode["run_id"]
            directory.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "word": word["word"],
                    "status": self.statuses[i % len(self.statuses)],
                    "note_id": 100 + i if self.statuses[i % len(self.statuses)] == "created" else None,
                }
                for i, word in enumerate(episode["words"])
            ]
            transport.write_json(
                directory / "result-1.json",
                {
                    "schema": 1,
                    "run_id": episode["run_id"],
                    "outcome": "success",
                    "words": rows,
                },
            )
            runs.append({"run_id": episode["run_id"], "ok": True, "file": "result-1.json"})
        return {"schema": 1, "ok": True, "error": None, "message": None, "runs": runs}

    def prepare(self):
        return miner.prepare(self.config, self.request, self.folder)

    def commit(self, ids=None):
        return miner.commit(self.config, self.folder, {"candidate_ids": ["c0001"] if ids is None else ids})

    def test_prepare_exports_small_shortlist_without_mining(self):
        summary = self.prepare()
        self.assertEqual(summary["max_cards"], 1)
        self.assertEqual(self.calls, [])
        shortlist = transport.read_json(self.folder / "shortlist.json")
        self.assertEqual(set(shortlist), {"candidates", "max_cards"})
        self.assertNotIn("target", shortlist)

    def test_commit_sends_only_selected_words_and_freezes_destination(self):
        self.prepare()
        receipt = self.commit()
        job = self.calls[0]
        self.assertEqual(job["config"]["anki_deck_name"], "Japanese")
        self.assertFalse(job["config"]["allow_duplicate_cards"])
        self.assertEqual(
            job["episodes"][0]["words"], [{"word": "study", "line_start": 1.0, "line_expansion": [0, 1]}]
        )
        self.assertEqual(receipt["outputs"][0]["note_id"], 100)
        self.assertTrue(receipt["ok"])

    def test_limit_unknown_and_duplicate_ids_fail_before_dispatch(self):
        self.prepare()
        for ids in (["c0001", "c0002"], ["unknown"], ["c0001", "c0001"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.commit(ids)
        self.assertEqual(self.calls, [])

    def test_zero_selections_succeed_without_contacting_upstream(self):
        self.prepare()
        result = self.commit([])
        self.assertEqual(result["counts"]["selected"], 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls, [])

    def test_completed_retry_does_not_write_again_or_require_unchanged_media(self):
        self.prepare()
        first = self.commit()
        self.video.write_bytes(b"changed after successful commit")
        self.assertEqual(self.commit(), first)
        self.assertEqual(len(self.calls), 1)

    def test_changed_selection_is_rejected_after_reservation(self):
        self.prepare()
        self.commit()
        with self.assertRaisesRegex(ValueError, "cannot change"):
            self.commit(["c0002"])
        self.assertEqual(len(self.calls), 1)

    def test_modified_source_fails_before_write(self):
        self.prepare()
        self.subtitles.write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.commit()
        self.assertEqual(self.calls, [])

    def test_config_change_requires_new_preparation(self):
        self.prepare()
        changed = {**self.config, "profile": "different"}
        with self.assertRaisesRegex(ValueError, "Configuration changed"):
            miner.commit(changed, self.folder, {"candidate_ids": ["c0001"]})
        self.assertEqual(self.calls, [])

    def test_concurrent_commit_cannot_enter_writer(self):
        self.prepare()
        with miner.run_lock(self.folder):
            with self.assertRaisesRegex(RuntimeError, "already being committed"):
                self.commit()
        self.assertEqual(self.calls, [])

    def test_lost_response_never_dispatches_again(self):
        self.prepare()
        self.failure = TimeoutError("lost response")
        self.assertEqual(self.commit()["status"], "uncertain")
        self.assertEqual(self.commit()["status"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_call_can_recover_existing_result_files(self):
        self.prepare()
        self.failure = TimeoutError("lost response")
        self.commit()
        self.write_results(self.calls[0])
        result = self.commit()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["created"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_global_refusal_is_not_mistaken_for_success(self):
        self.prepare()
        with patch(
            "miner.upstream",
            return_value={
                "schema": 1,
                "ok": False,
                "error": "BUSY",
                "message": "Close the window",
                "runs": [],
            },
        ):
            result = self.commit()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["outputs"][0]["status"], "not_attempted")

    def test_partial_batch_reports_each_word(self):
        self.request["max_cards"] = 2
        self.statuses = ["created", "media_failed"]
        self.prepare()
        result = self.commit(["c0001", "c0002"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["created"], 1)
        self.assertEqual(result["counts"]["failed"], 1)

    def test_malformed_upstream_result_stops_replay(self):
        self.prepare()
        self.statuses = ["invented_status"]
        self.assertEqual(self.commit()["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "Invalid per-word"):
            self.commit()
        self.assertEqual(len(self.calls), 1)

    def test_invalid_limit_and_offset_fail_before_external_calls(self):
        for limit in (True, 0, -1, 1001, "10"):
            request = {**self.request, "max_cards": limit}
            with self.assertRaises(ValueError):
                miner.prepare(self.config, request, self.folder)
        request = copy.deepcopy(self.request)
        request["inputs"][0]["subtitle_offset"] = float("nan")
        with self.assertRaises(ValueError):
            miner.prepare(self.config, request, self.folder)
        self.api.assert_not_called()
        self.bridge.assert_not_called()

    def test_upstream_settings_change_requires_new_preparation(self):
        self.prepare()
        self.prepared["upstream_state"]["settings_hash"] = "changed"
        with self.assertRaisesRegex(ValueError, "Upstream version or profile settings changed"):
            self.commit()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.folder / "receipt.json").exists())

    def test_failed_discovery_does_not_publish_a_run(self):
        self.bridge.side_effect = RuntimeError("Upstream filter failed")
        with self.assertRaisesRegex(RuntimeError, "filter failed"):
            self.prepare()
        self.assertFalse(self.folder.exists())

    def test_old_runs_require_new_preparation(self):
        self.prepare()
        run = transport.read_json(self.folder / "run.json")
        run["schema"] = 1
        transport.write_json(self.folder / "run.json", run)
        with self.assertRaisesRegex(ValueError, "outdated"):
            self.commit()
        self.assertEqual(self.calls, [])


class BoundaryTests(unittest.TestCase):
    def test_upstream_command_is_shell_free_and_scrubs_pythonpath(self):
        config = {"upstream_python": "C:/Program Files/AnkiMiner/python.exe"}
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict("os.environ", {"PYTHONPATH": "injected"}):
                with patch(
                    "transport.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, b'{"schema":1,"ok":false,"error":"BUSY"}'
                    ),
                ) as call:
                    verdict = transport.upstream(config, "mine", "request.json", log=Path(folder) / "log")
            self.assertFalse(verdict["ok"])
            self.assertEqual(
                call.call_args.args[0],
                [config["upstream_python"], "-I", "-m", "anki_miner", "--api", "mine", "request.json"],
            )
            self.assertNotIn("PYTHONPATH", call.call_args.kwargs["env"])
            self.assertFalse(call.call_args.kwargs.get("shell", False))

    def test_removed_settings_require_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            transport.write_json(path, {"known_words": []})
            with self.assertRaisesRegex(ValueError, "Unknown keys"):
                miner.load_config(path)


if __name__ == "__main__":
    unittest.main()
