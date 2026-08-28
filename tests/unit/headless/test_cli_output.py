import json

from anki_miner.headless.cli import main


class _App:
    def commit_mining_run(self, run_id, reviews):
        assert run_id == "run-1"
        assert reviews == []
        return {
            "job_id": "job-1",
            "state": "partial",
            "review_counts": {"reviewed": 2, "selected": 2, "rejected": 0},
            "counts": {"selected": 2, "created": 1, "duplicate_skipped": 0, "failed": 1},
            "destination": {"deck": "Mining", "note_type": "Note"},
            "enrichment_coverage": {"chosen_definition": 2},
            "shortfall": 0,
            "job_tag_query": 'tag:"job"',
            "outputs": [
                {"candidate_id": "a", "outcome": "created", "note_id": 1},
                {"candidate_id": "b", "outcome": "failed", "error": {"code": "fixture"}},
            ],
        }

    def close(self):
        pass


def test_commit_run_writes_receipt_and_prints_compact_summary(tmp_path, monkeypatch, capsys):
    request = tmp_path / "reviews.json"
    request.write_text(json.dumps({"run_id": "run-1", "reviews": []}), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr("anki_miner.headless.cli.build_agent_application", lambda _config: _App())

    exit_code = main(
        [
            "--config",
            str(tmp_path / "config.json"),
            "commit-run",
            "--request",
            str(request),
            "--output",
            str(receipt),
            "--summary",
        ]
    )

    assert exit_code == 7
    full = json.loads(receipt.read_text(encoding="utf-8"))
    summary = json.loads(capsys.readouterr().out)
    assert len(full["result"]["outputs"]) == 2
    assert "outputs" not in summary["result"]
    assert summary["result"]["failures"] == [{"candidate_id": "b", "error": {"code": "fixture"}}]
