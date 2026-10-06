"""Real CLI processes against an isolated HTTP fixture and fake upstream executable."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from transport import read_json, write_json

FAKE_UPSTREAM = r"""
import json, sys
from pathlib import Path
assert sys.argv[1] == "--api"
command = sys.argv[2]
verdict = {"schema": 1, "ok": True, "error": None, "message": None}
if command == "version":
    verdict["result"] = {"schema": 1, "app": "fixture", "commands": ["mine"]}
elif command == "check":
    verdict["result"] = {"ready": True, "items": []}
elif command == "settings-export":
    Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps({
        "anki_miner_settings": 1, "configured": True, "settings": {
            "anki_deck_name": "Test", "anki_note_type": "Basic", "anki_fields": {"word": "Front"}
        }
    }), encoding="utf-8")
elif command == "mine":
    job = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    assert job["schema"] == 1 and job["language"] == "ja"
    assert not job["config"]["allow_duplicate_cards"]
    verdict["runs"] = []
    for episode in job["episodes"]:
        directory = Path(job["run_dir"]) / episode["run_id"]
        directory.mkdir()
        result = {"schema": 1, "run_id": episode["run_id"], "outcome": "success", "words": [
            {"word": word["word"], "status": "created", "note_id": 123 + i, "media_missing": []}
            for i, word in enumerate(episode["words"])
        ]}
        (directory / "result-1.json").write_text(json.dumps(result), encoding="utf-8")
        verdict["runs"].append({"run_id": episode["run_id"], "ok": True, "file": "result-1.json"})
else:
    raise AssertionError(command)
print(json.dumps(verdict))
"""


class CliTests(unittest.TestCase):
    def test_file_backed_workflow_and_retry_in_real_processes(self):
        actions = []

        class AnkiFixture(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                actions.append(payload["action"])
                response = {"result": [], "error": None}
                if payload["action"] != "findNotes":
                    response["error"] = "Unexpected operation; this fixture only reads known words"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), AnkiFixture) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    fake = folder / "upstream.py"
                    fake.write_text(FAKE_UPSTREAM, encoding="utf-8")
                    config = folder / "config.json"
                    write_json(
                        config,
                        {
                            "upstream": [sys.executable, str(fake)],
                            "anki_connect": f"http://127.0.0.1:{server.server_port}",
                        },
                    )
                    command = [
                        sys.executable,
                        str(Path(__file__).parents[1] / "miner.py"),
                        "--config",
                        str(config),
                    ]

                    def invoke(*args):
                        result = subprocess.run(
                            [*command, *map(str, args)], capture_output=True, text=True, timeout=15
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        return json.loads(result.stdout)

                    self.assertTrue(invoke("doctor")["ok"])
                    video, subtitles = folder / "episode.mp4", folder / "episode.srt"
                    video.write_bytes(b"fixture")
                    subtitles.write_text(
                        "1\n00:00:01,000 --> 00:00:03,000\n日本語を勉強する。\n", encoding="utf-8"
                    )
                    request = folder / "prepare.json"
                    write_json(
                        request,
                        {
                            "max_cards": 1,
                            "inputs": [{"video_file": str(video), "subtitle_file": str(subtitles)}],
                        },
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
                    self.assertEqual(actions, ["findNotes"])
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_missing_upstream_is_a_compact_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            write_json(config, {"upstream": [str(Path(temporary) / "does-not-exist.exe")]})
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
