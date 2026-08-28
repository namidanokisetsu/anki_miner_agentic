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
        **kwargs,
    )


def make_run(
    store,
    tmp_path,
    candidate_ids=("candidate_z", "candidate_a", "candidate_m"),
    audio_tracks=None,
    run_candidate_ids=None,
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
                    "review": {"state": "required", "contract_version": "candidate_review_v1"},
                    "definition_options": [
                        {"option_id": "definition_1", "dictionary": "D", "text": f"meaning-{index}"}
                    ],
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
        },
        rows,
    )
    published = set(candidate_ids if run_candidate_ids is None else run_candidate_ids)
    return store.create_run("batch_two_call", [row["public"] for row in rows if row["public"]["candidate_id"] in published])


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


def review(candidate_id, *, decision="select", reason_code=None, enrichments=None):
    item = {
        "candidate_id": candidate_id,
        "decision": decision,
        "definition_option_id": "definition_1" if decision == "select" else None,
        "reason_code": reason_code or ("clear_supported_target" if decision == "select" else "ambiguous_context"),
    }
    if enrichments is not None:
        item["enrichments"] = enrichments
    return item


def test_two_call_commit_groups_and_preserves_exact_order_and_receipts(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path)
    writer = BatchWriter()
    service = MiningCommitService(store, config(), writer)
    reviews = [review(value) for value in ("candidate_z", "candidate_a", "candidate_m")]

    receipt = service.commit_run(run["run_id"], reviews)

    assert writer.preflights == 1
    assert len(writer.groups) == 1
    assert receipt["counts"] == {"selected": 3, "created": 1, "duplicate_skipped": 1, "failed": 1}
    assert [item["candidate_id"] for item in receipt["outputs"]] == [
        "candidate_z",
        "candidate_a",
        "candidate_m",
    ]
    assert receipt["outputs"][0]["note_id"] == 901
    assert "selection" not in receipt
    assert all("media" not in item and "review_state" not in item for item in receipt["outputs"])
    assert receipt["tags"][0] == "anki_miner_agentic"
    assert receipt["tags"][1].startswith("anki_miner_agentic::job::")
    assert receipt["job_tag_query"] == f'tag:"{receipt["tags"][1]}"'
    assert receipt["destination"] == {"deck": "Mining", "note_type": "Note"}


def test_unchanged_retry_reuses_job_and_changed_selection_fails(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()
    service = MiningCommitService(store, config(), writer)
    chosen = [review("candidate_z")]

    first = service.commit_run(run["run_id"], chosen)
    second = service.commit_run(run["run_id"], chosen)
    assert second["job_id"] == first["job_id"]
    assert second["tags"] == first["tags"]
    assert len(writer.groups) == 1

    with pytest.raises(AgentMiningError) as raised:
        service.commit_run(run["run_id"], [review("candidate_z", decision="reject")])
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
            [review("candidate_z", enrichments={"chosen_definition": "meaning-0"})],
        )
    assert raised.value.code == "missing_required_enrichment"
    assert writer.preflights == 0
    assert writer.groups == []
    assert store.run_status(run["run_id"])["committed_job_id"] is None


def test_two_call_review_accepts_contextual_definition_paraphrase(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()
    service = MiningCommitService(store, config(chosen_definition_field="Chosen"), writer)

    receipt = service.commit_run(
        run["run_id"],
        [review("candidate_z", enrichments={"chosen_definition": "contextual paraphrase"})],
    )

    assert receipt["counts"]["created"] == 1


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
    service.commit_run(
        run["run_id"],
        [
            review("candidate_z"),
            review("candidate_a"),
            review("candidate_m", decision="reject"),
        ],
    )

    assert len(calls) == 2
    assert len(set(calls)) == 2


def test_prepare_run_syncs_and_consumes_storage_pages_internally(tmp_path, monkeypatch):
    monkeypatch.setattr("anki_miner.agent.application._CANDIDATE_PAGE_SIZE", 1)
    store = AgentStore(tmp_path / "agent.sqlite3")
    events = []

    class Profile:
        def sync(self):
            events.append("sync")
            return {"status": "ready"}

    class Candidates:
        def prepare(self, inputs, *, max_cards):
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
                },
                rows,
            )

    app = AgentMiningApplication(
        store,
        AgentProfileConfig(
            (KnowledgeSource("Known", "Note", ("Expression",), ()),),
            WriteTarget("Mining", "Note"),
        ),
        Profile(),
        Candidates(),
        object(),
    )

    result = app.prepare_mining_run({"inputs": [], "max_cards": 3})

    assert events == ["sync", "prepare"]
    assert result["run_id"].startswith("run_")
    assert [item["candidate_id"] for item in result["shortlist"]] == [
        "candidate-0",
        "candidate-1",
        "candidate-2",
    ]
    assert result["review_contract"] == {
        "version": "candidate_review_v1",
        "instruction": result["review_contract"]["instruction"],
        "decisions": ["select", "reject"],
        "select_reason": "clear_supported_target",
        "reject_reasons": ["ambiguous_context", "suspicious_text", "unsupported_sense"],
        "fields": ["candidate_id", "decision", "definition_option_id", "reason_code"],
        "optional_fields": ["rationale", "enrichments"],
    }


def test_prepare_run_projects_only_semantic_review_fields():
    from anki_miner.agent.application import _review_projection

    projected = _review_projection(
        {
            "candidate_id": "candidate-1",
            "target": {"surface": "食べた", "mined_form": "食べる", "reading": "たべる", "pos": "verb"},
            "sentence": {"text": "寿司を食べた。", "chars": 8, "unknown_lexemes": 1},
            "definition_options": [{"option_id": "definition_1", "dictionary": "D", "text": "to eat"}],
            "allowed_enrichments": ["chosen_definition", "sentence_translation"],
            "flags": ["automatic_transcript"],
            "learner": {"word_exposures": 10},
            "signals": {"frequency_rank": 100},
            "pitch": {"position": 1},
            "episode": {"subtitle_source": "fixture"},
            "variants": [{"sentence": "別の文"}],
            "eligibility": {"diagnostics": []},
        }
    )

    assert projected == {
        "candidate_id": "candidate-1",
        "target": {"surface": "食べた", "mined_form": "食べる", "reading": "たべる", "pos": "verb"},
        "sentence": {"text": "寿司を食べた。"},
        "definition_options": [{"option_id": "definition_1", "dictionary": "D", "text": "to eat"}],
        "allowed_enrichments": ["chosen_definition", "sentence_translation"],
        "flags": ["automatic_transcript"],
    }


def test_requested_max_cards_is_the_only_review_batch_bound(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")

    class Profile:
        def sync(self):
            return {"status": "ready"}

    class Candidates:
        def prepare(self, inputs, *, max_cards):
            rows = [
                {
                    "lexical_id": f"word-{index}",
                    "public": {
                        "candidate_id": f"candidate-{index}",
                        "eligible": True,
                        "sentence": {"text": "文" * 150},
                    },
                    "internal": {},
                }
                for index in range(40)
            ]
            return store.create_batch(
                {
                    "revision_id": "batch_large_ceiling",
                    "profile_revision_id": "profile",
                    "analyzer_key": "a",
                    "config_hash": "c",
                    "request_hash": "r",
                    "sources": [],
                    "max_cards": max_cards,
                },
                rows,
            )

    app = AgentMiningApplication(
        store,
        AgentProfileConfig(
            (KnowledgeSource("Known", "Note", ("Expression",), ()),),
            WriteTarget("Mining", "Note"),
        ),
        Profile(),
        Candidates(),
        object(),
    )

    result = app.prepare_mining_run({"inputs": [], "max_cards": 300})

    assert result["max_cards"] == 300
    assert result["review_batch"]["count"] == 40
    assert result["review_batch"]["zero_or_shortfall_is_success"] is True
    assert result["review_batch"]["complete"] is True


def test_decorative_score_is_not_part_of_the_review_contract(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()

    with pytest.raises(AgentMiningError) as raised:
        MiningCommitService(store, config(), writer).commit_run(
            run["run_id"],
            [{"candidate_id": "candidate_z", "metadata": {"score": 0.9}}],
        )

    assert raised.value.code == "invalid_review"
    assert writer.preflights == 0


def test_all_rejected_is_a_successful_no_write_review(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z",))
    writer = BatchWriter()

    receipt = MiningCommitService(store, config(), writer).commit_run(
        run["run_id"], [review("candidate_z", decision="reject", reason_code="suspicious_text")]
    )

    assert receipt["state"] == "completed"
    assert receipt["review_counts"] == {"reviewed": 1, "selected": 0, "rejected": 1}
    assert writer.preflights == 0


def test_every_returned_candidate_requires_an_explicit_review(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(store, tmp_path, ("candidate_z", "candidate_a"))
    writer = BatchWriter()

    with pytest.raises(AgentMiningError) as raised:
        MiningCommitService(store, config(), writer).commit_run(run["run_id"], [review("candidate_z")])

    assert raised.value.code == "missing_candidate_reviews"
    assert writer.preflights == 0


def test_run_feedback_records_only_candidates_published_for_review(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    run = make_run(
        store,
        tmp_path,
        ("candidate_z", "candidate_a", "candidate_m"),
        run_candidate_ids=("candidate_z",),
    )

    MiningCommitService(store, config(), BatchWriter()).commit_run(run["run_id"], [review("candidate_z")])

    with sqlite3.connect(tmp_path / "agent.sqlite3") as conn:
        feedback = conn.execute(
            "SELECT candidate_id, decision FROM candidate_feedback ORDER BY candidate_id"
        ).fetchall()
    assert feedback == [("candidate_z", "selected")]


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
        run["run_id"], [review("candidate_z"), review("candidate_a")]
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert {"job_timestamp", "job_tag"} <= columns


def test_store_migrates_v2_review_pool_column(tmp_path):
    path = tmp_path / "agent.sqlite3"
    AgentStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE mining_batches ADD COLUMN review_pool_size INTEGER")
        conn.execute("PRAGMA user_version=2")

    AgentStore(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mining_batches)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "review_pool_size" not in columns
