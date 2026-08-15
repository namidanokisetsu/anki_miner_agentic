from __future__ import annotations

import sqlite3

import pytest

from anki_miner.agent.application import AgentMiningApplication
from anki_miner.agent.candidates import file_fingerprint
from anki_miner.agent.commit import MiningCommitService
from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig, KnowledgeSource, WriteTarget
from anki_miner.agent.store import AgentStore


def config(**kwargs):
    return AgentProfileConfig(
        (KnowledgeSource("Known", "Note", ("Expression",), ("Sentence",)),),
        WriteTarget("Mining", "Note", enabled=True),
        max_cards=3,
        **kwargs,
    )


def make_run(
    store,
    tmp_path,
    candidate_ids=("candidate_z", "candidate_a", "candidate_m"),
    audio_tracks=None,
):
    video = tmp_path / "episode.mp4"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("fixture", encoding="utf-8")
    rows = []
    for index, candidate_id in enumerate(candidate_ids):
        rows.append(
            {
                "lexical_id": f"word-{index}",
                "public": {
                    "candidate_id": candidate_id,
                    "eligible": True,
                    "definition_options": [{"dictionary": "D", "text": f"meaning-{index}"}],
                },
                "internal": {
                    "video_fingerprint": file_fingerprint(video),
                    "subtitle_fingerprint": file_fingerprint(subtitle),
                    "audio_track": (audio_tracks[index] if audio_tracks is not None else "japanese"),
                },
            }
        )
    store.create_batch(
        {
            "revision_id": "batch_two_call",
            "profile_revision_id": "profile",
            "analyzer_key": "analyzer",
            "config_hash": "config",
            "request_hash": "request",
            "sources": [],
            "max_cards": 3,
            "review_pool_size": 3,
        },
        rows,
    )
    return store.create_run("batch_two_call", [row["public"] for row in rows])


class BatchWriter:
    def __init__(self):
        self.preflights = 0
        self.groups = []

    def preflight(self):
        self.preflights += 1

    def create_batch(self, candidates, tags):
        self.groups.append(([item["candidate_id"] for item in candidates], tags))
        return [
            {"outcome": "created", "note_id": 901},
            {"outcome": "duplicate_skipped", "note_id": None},
            {"outcome": "failed", "error": {"code": "fixture", "message": "failed"}},
        ][: len(candidates)]


def test_two_call_commit_groups_and_preserves_exact_order_and_receipts(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path)
    writer = BatchWriter()
    service = MiningCommitService(store, config(), writer)
    selections = [{"candidate_id": value} for value in ("candidate_z", "candidate_a", "candidate_m")]

    receipt = service.commit_run(run["run_id"], selections)

    assert writer.preflights == 1
    assert len(writer.groups) == 1
    assert receipt["counts"] == {"selected": 3, "created": 1, "duplicate_skipped": 1, "failed": 1}
    assert [item["candidate_id"] for item in receipt["outputs"]] == [
        "candidate_z",
        "candidate_a",
        "candidate_m",
    ]
    assert receipt["outputs"][0]["note_id"] == 901
    assert receipt["tags"][0] == "anki_miner_agentic"
    assert receipt["tags"][1].startswith("anki_miner_agentic::job::")
    assert receipt["job_tag_query"] == f'tag:"{receipt["tags"][1]}"'
    assert receipt["destination"] == {"deck": "Mining", "note_type": "Note"}


def test_unchanged_retry_reuses_job_and_changed_selection_fails(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()
    service = MiningCommitService(store, config(), writer)
    selection = [{"candidate_id": "candidate_z"}]

    first = service.commit_run(run["run_id"], selection)
    second = service.commit_run(run["run_id"], selection)
    assert second["job_id"] == first["job_id"]
    assert second["tags"] == first["tags"]
    assert len(writer.groups) == 1

    with pytest.raises(AgentMiningError) as raised:
        service.commit_run(run["run_id"], [])
    assert raised.value.code == "run_selection_changed"
    assert len(writer.groups) == 1


def test_required_enrichment_validation_is_side_effect_free(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()
    service = MiningCommitService(
        store,
        config(chosen_definition_field="Chosen", sentence_translation_field="Translation"),
        writer,
    )

    with pytest.raises(AgentMiningError) as raised:
        service.commit_run(
            run["run_id"],
            [{"candidate_id": "candidate_z", "enrichments": {"chosen_definition": "meaning-0"}}],
        )
    assert raised.value.code == "missing_required_enrichment"
    assert writer.preflights == 0
    assert writer.groups == []
    assert store.run_status(run["run_id"])["committed_job_id"] is None


def test_commit_fingerprints_each_unique_path_once(monkeypatch, tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path)
    writer = BatchWriter()
    service = MiningCommitService(store, config(), writer)
    from anki_miner.agent import commit as commit_module

    real = commit_module.file_fingerprint
    calls = []

    def counted(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(commit_module, "file_fingerprint", counted)
    service.commit_run(run["run_id"], [{"candidate_id": value} for value in ("candidate_z", "candidate_a")])

    assert len(calls) == 2
    assert len(set(calls)) == 2


def test_prepare_run_syncs_and_consumes_storage_pages_internally(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    events = []

    class Profile:
        def sync(self):
            events.append("sync")
            return {"status": "ready"}

    class Candidates:
        def prepare(self, inputs, *, max_cards, review_pool_size):
            events.append("prepare")
            rows = [
                {
                    "lexical_id": f"word-{index}",
                    "public": {"candidate_id": f"candidate-{index}", "eligible": True},
                    "internal": {},
                }
                for index in range(3)
            ]
            return store.create_batch(
                {
                    "revision_id": "batch_pages",
                    "profile_revision_id": "profile",
                    "analyzer_key": "a",
                    "config_hash": "c",
                    "request_hash": "r",
                    "sources": [],
                    "max_cards": max_cards,
                    "review_pool_size": review_pool_size,
                },
                rows,
            )

    app = AgentMiningApplication(
        store,
        AgentProfileConfig(
            (KnowledgeSource("Known", "Note", ("Expression",), ()),),
            WriteTarget("Mining", "Note"),
            max_cards=3,
            page_size=1,
            max_payload_bytes=10_000,
        ),
        Profile(),
        Candidates(),
        object(),
    )

    result = app.prepare_mining_run({"inputs": [], "max_cards": 3, "review_pool_size": 3})

    assert events == ["sync", "prepare"]
    assert result["run_id"].startswith("run_")
    assert [item["candidate_id"] for item in result["shortlist"]] == [
        "candidate-0",
        "candidate-1",
        "candidate-2",
    ]


def test_global_writer_failure_stops_remaining_source_groups(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(
        store,
        tmp_path,
        ("candidate_z", "candidate_a"),
        audio_tracks=("japanese", 1),
    )

    class FailingWriter(BatchWriter):
        def create_batch(self, candidates, tags):
            self.groups.append(([item["candidate_id"] for item in candidates], tags))
            raise AgentMiningError("schema_changed", "note type changed")

    writer = FailingWriter()
    receipt = MiningCommitService(store, config(), writer).commit_run(
        run["run_id"], [{"candidate_id": "candidate_z"}, {"candidate_id": "candidate_a"}]
    )

    assert len(writer.groups) == 1
    assert receipt["counts"]["failed"] == 2
    assert all(item["error"]["global"] for item in receipt["outputs"])


def test_store_migrates_v1_jobs_with_durable_tag_columns(tmp_path):
    path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE mining_jobs (
               job_id TEXT PRIMARY KEY, batch_revision TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               state TEXT NOT NULL, selection_json TEXT NOT NULL, error_json TEXT)"""
        )
        conn.execute("PRAGMA user_version=1")

    AgentStore(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mining_jobs)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert {"job_timestamp", "job_tag"} <= columns
