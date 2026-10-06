"""Real CLI processes with isolated upstream boundary fixtures."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from transport import read_json, write_json

FIXTURE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
import miner
from transport import read_json, write_json

def bridge(config, command, **kwargs):
    return {"upstream_state": {"version": "fixture", "settings_hash": "fixture"},
            "target": {"anki_deck_name": "Test", "anki_note_type": "Basic",
                       "anki_fields": {"word": "Front"}, "allow_duplicate_cards": False},
            "total_candidates": 1,
            "candidates": [{"id": "c0001", "word": "test", "sentence": "test sentence",
                            "source": 0, "line_start": 1.0, "line_expansion": [0, 0]}]}

def upstream(config, command, *args, **kwargs):
    if command == "version":
        return {"schema": 1, "ok": True, "result": {"schema": 1, "app": "fixture", "commands": ["mine"]}}
    if command == "check":
        return {"schema": 1, "ok": True, "result": {"ready": True, "items": []}}
    assert command == "mine"
    job = read_json(args[0])
    assert not job["config"]["allow_duplicate_cards"]
    for episode in job["episodes"]:
        directory = Path(job["run_dir"]) / episode["run_id"]
        directory.mkdir()
        write_json(directory / "result-1.json", {
            "schema": 1, "run_id": episode["run_id"], "outcome": "success", "words": [
                {"word": word["word"], "status": "created", "note_id": 123 + i}
                for i, word in enumerate(episode["words"])
            ]})
    return {"schema": 1, "ok": True}

miner.bridge = bridge
miner.upstream = upstream
raise SystemExit(miner.main())
"""


class CliTests(unittest.TestCase):
    def test_file_backed_workflow_and_retry_in_real_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            launcher = folder / "fixture.py"
            launcher.write_text(FIXTURE, encoding="utf-8")
            config = folder / "config.json"
            write_json(config, {})
            command = [sys.executable, str(launcher), str(Path(__file__).parents[1]), "--config", str(config)]

            def invoke(*args):
                result = subprocess.run(
                    [*command, *map(str, args)], capture_output=True, text=True, timeout=15
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            self.assertTrue(invoke("doctor")["ok"])
            video, subtitles = folder / "episode.mp4", folder / "episode.srt"
            video.write_bytes(b"fixture")
            subtitles.write_text("fixture", encoding="utf-8")
            request = folder / "prepare.json"
            write_json(
                request,
                {"max_cards": 1, "inputs": [{"video_file": str(video), "subtitle_file": str(subtitles)}]},
            )
            run = folder / "run"
            self.assertTrue(invoke("prepare", "--request", request, "--out", run)["ok"])
            shortlist = read_json(run / "shortlist.json")
            selection = folder / "selection.json"
            write_json(selection, {"candidate_ids": [shortlist["candidates"][0]["id"]]})
            first = invoke("commit", "--run", run, "--selection", selection)
            second = invoke("commit", "--run", run, "--selection", selection)
            self.assertEqual(first, second)
            self.assertEqual(first["counts"]["created"], 1)
            self.assertEqual(read_json(run / "receipt.json")["outputs"][0]["note_id"], 123)

    def test_missing_upstream_is_a_compact_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            write_json(config, {"upstream_python": str(Path(temporary) / "does-not-exist.exe")})
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "miner.py"),
                    "--config",
                    str(config),
                    "doctor",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Install upstream", json.loads(result.stdout)["error"])
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
