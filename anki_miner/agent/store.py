"""Transactional SQLite storage for profiles, batches, jobs, and feedback."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import AgentMiningError, require
from .models import PUBLIC_SCHEMA_VERSION, canonical_json, content_id

_SCHEMA_VERSION = 1


class AgentStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, _SCHEMA_VERSION):
                raise AgentMiningError(
                    "unsupported_database",
                    f"Agent database schema {version} is not supported by this build",
                )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS profile_revisions (
                    revision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    analyzer_key TEXT NOT NULL,
                    analyzer_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    note_count INTEGER NOT NULL,
                    card_count INTEGER NOT NULL,
                    published INTEGER NOT NULL CHECK (published IN (0, 1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_published_profile
                    ON profile_revisions(published) WHERE published = 1;
                CREATE TABLE IF NOT EXISTS source_notes (
                    note_id INTEGER PRIMARY KEY,
                    revision_id TEXT NOT NULL REFERENCES profile_revisions(revision_id) ON DELETE CASCADE,
                    model_name TEXT NOT NULL,
                    deck_name TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_fields (
                    note_id INTEGER NOT NULL REFERENCES source_notes(note_id) ON DELETE CASCADE,
                    field_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('word', 'text')),
                    cleaned_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(note_id, field_name)
                );
                CREATE TABLE IF NOT EXISTS field_tokens (
                    note_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    token_index INTEGER NOT NULL,
                    lexical_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    lemma TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    pos TEXT NOT NULL,
                    subtype TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    PRIMARY KEY(note_id, field_name, token_index),
                    FOREIGN KEY(note_id, field_name) REFERENCES source_fields(note_id, field_name) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS field_tokens_lexical ON field_tokens(lexical_id);
                CREATE TABLE IF NOT EXISTS source_cards (
                    card_id INTEGER PRIMARY KEY,
                    note_id INTEGER NOT NULL REFERENCES source_notes(note_id) ON DELETE CASCADE,
                    deck_name TEXT NOT NULL,
                    interval_days INTEGER NOT NULL,
                    reps INTEGER NOT NULL,
                    lapses INTEGER NOT NULL,
                    queue INTEGER,
                    card_type INTEGER
                );
                CREATE TABLE IF NOT EXISTS lexical_state (
                    lexical_id TEXT PRIMARY KEY,
                    word_exposures INTEGER NOT NULL,
                    sentence_exposures INTEGER NOT NULL,
                    card_count INTEGER NOT NULL,
                    reps INTEGER NOT NULL,
                    lapses INTEGER NOT NULL,
                    interval_days INTEGER,
                    state TEXT NOT NULL,
                    analyzer_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mining_batches (
                    revision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    profile_revision_id TEXT NOT NULL,
                    analyzer_key TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('ready', 'committing', 'completed', 'failed')),
                    source_json TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    eligible_count INTEGER NOT NULL,
                    max_cards INTEGER NOT NULL,
                    review_pool_size INTEGER,
                    committed_job_id TEXT,
                    error_json TEXT
                );
                CREATE TABLE IF NOT EXISTS mining_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    batch_revision TEXT NOT NULL REFERENCES mining_batches(revision_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    lexical_id TEXT NOT NULL,
                    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
                    public_json TEXT NOT NULL,
                    internal_json TEXT NOT NULL,
                    UNIQUE(batch_revision, position),
                    UNIQUE(batch_revision, lexical_id)
                );
                CREATE INDEX IF NOT EXISTS mining_candidates_batch ON mining_candidates(batch_revision, position);
                CREATE TABLE IF NOT EXISTS mining_jobs (
                    job_id TEXT PRIMARY KEY,
                    batch_revision TEXT NOT NULL REFERENCES mining_batches(revision_id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'completed', 'partial', 'failed', 'cancelled')),
                    selection_json TEXT NOT NULL,
                    error_json TEXT
                );
                CREATE TABLE IF NOT EXISTS mining_outputs (
                    job_id TEXT NOT NULL REFERENCES mining_jobs(job_id) ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL REFERENCES mining_candidates(candidate_id),
                    outcome TEXT NOT NULL CHECK (outcome IN ('created', 'duplicate_skipped', 'failed')),
                    note_id INTEGER,
                    media_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT,
                    review_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(job_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS candidate_feedback (
                    batch_revision TEXT NOT NULL REFERENCES mining_batches(revision_id) ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL REFERENCES mining_candidates(candidate_id),
                    decision TEXT NOT NULL CHECK (decision IN ('selected', 'rejected', 'not_reviewed')),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    output_outcome TEXT,
                    PRIMARY KEY(batch_revision, candidate_id)
                );
                """)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def publish_profile(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Atomically replace the published profile after all data is validated."""
        with self._transaction() as conn:
            current = conn.execute("SELECT revision_id FROM profile_revisions WHERE published=1").fetchone()
            if current is not None and current["revision_id"] == snapshot["revision_id"]:
                conn.execute(
                    "UPDATE profile_revisions SET created_at=CURRENT_TIMESTAMP WHERE revision_id=?",
                    (snapshot["revision_id"],),
                )
                return self.profile_status()
            conn.execute("UPDATE profile_revisions SET published=0 WHERE published=1")
            conn.execute("DELETE FROM lexical_state")
            conn.execute("DELETE FROM source_notes")
            conn.execute("DELETE FROM profile_revisions WHERE revision_id=?", (snapshot["revision_id"],))
            conn.execute(
                """INSERT INTO profile_revisions
                   (revision_id, analyzer_key, analyzer_json, config_hash, capabilities_json,
                    note_count, card_count, published)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    snapshot["revision_id"],
                    snapshot["analyzer_key"],
                    canonical_json(snapshot["analyzer"]),
                    snapshot["config_hash"],
                    canonical_json(snapshot["capabilities"]),
                    len(snapshot["notes"]),
                    len(snapshot["cards"]),
                ),
            )
            for note in snapshot["notes"]:
                conn.execute(
                    "INSERT INTO source_notes VALUES (?, ?, ?, ?, ?)",
                    (
                        note["note_id"],
                        snapshot["revision_id"],
                        note["model_name"],
                        note["deck_name"],
                        note["content_hash"],
                    ),
                )
                for field in note["fields"]:
                    conn.execute(
                        "INSERT INTO source_fields VALUES (?, ?, ?, ?, ?)",
                        (
                            note["note_id"],
                            field["field_name"],
                            field["role"],
                            field["cleaned_text"],
                            field["content_hash"],
                        ),
                    )
                    for index, token in enumerate(field["tokens"]):
                        conn.execute(
                            """INSERT INTO field_tokens VALUES
                               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                note["note_id"],
                                field["field_name"],
                                index,
                                token["lexical_id"],
                                token["surface"],
                                token["lemma"],
                                token["reading"],
                                token["pos"],
                                token["subtype"],
                                token["start"],
                                token["end"],
                            ),
                        )
            for card in snapshot["cards"]:
                conn.execute(
                    "INSERT INTO source_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        card["card_id"],
                        card["note_id"],
                        card["deck_name"],
                        card["interval_days"],
                        card["reps"],
                        card["lapses"],
                        card.get("queue"),
                        card.get("card_type"),
                    ),
                )
            for lexical in snapshot["lexical_state"]:
                conn.execute(
                    "INSERT INTO lexical_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lexical["lexical_id"],
                        lexical["word_exposures"],
                        lexical["sentence_exposures"],
                        lexical["card_count"],
                        lexical["reps"],
                        lexical["lapses"],
                        lexical["interval_days"],
                        lexical["state"],
                        snapshot["analyzer_key"],
                    ),
                )
            cards_by_note: dict[int, list[dict[str, Any]]] = {}
            for card in snapshot["cards"]:
                cards_by_note.setdefault(card["note_id"], []).append(card)
            for note_id, note_cards in cards_by_note.items():
                conn.execute(
                    "UPDATE mining_outputs SET review_json=? WHERE note_id=?",
                    (canonical_json({"cards": note_cards}), note_id),
                )
        return self.profile_status()

    def profile_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM profile_revisions WHERE published=1").fetchone()
            if row is None:
                return {"schema_version": PUBLIC_SCHEMA_VERSION, "status": "missing"}
            return {
                "schema_version": PUBLIC_SCHEMA_VERSION,
                "status": "ready",
                "revision_id": row["revision_id"],
                "created_at": row["created_at"],
                "analyzer": json.loads(row["analyzer_json"]),
                "analyzer_key": row["analyzer_key"],
                "config_hash": row["config_hash"],
                "capabilities": json.loads(row["capabilities_json"]),
                "note_count": row["note_count"],
                "card_count": row["card_count"],
            }

    def lexical_features(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            return {
                row["lexical_id"]: {
                    "state": row["state"],
                    "word_exposures": row["word_exposures"],
                    "sentence_exposures": row["sentence_exposures"],
                    "word_card_count": row["card_count"],
                    "reviews": row["reps"],
                    "lapses": row["lapses"],
                    "interval_days": row["interval_days"],
                }
                for row in conn.execute("SELECT * FROM lexical_state ORDER BY lexical_id")
            }

    def create_batch(self, batch: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT revision_id, state FROM mining_batches WHERE revision_id=?", (batch["revision_id"],)
            ).fetchone()
            if existing is not None:
                require(existing["state"] != "failed", "batch_failed", "An identical batch previously failed")
                return self.batch_status(batch["revision_id"], conn=conn)
            conn.execute(
                """INSERT INTO mining_batches
                   (revision_id, profile_revision_id, analyzer_key, config_hash, request_hash, state,
                    source_json, candidate_count, eligible_count, max_cards, review_pool_size)
                   VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (
                    batch["revision_id"],
                    batch["profile_revision_id"],
                    batch["analyzer_key"],
                    batch["config_hash"],
                    batch["request_hash"],
                    canonical_json(batch["sources"]),
                    len(candidates),
                    sum(1 for item in candidates if item["public"]["eligible"]),
                    batch["max_cards"],
                    batch.get("review_pool_size"),
                ),
            )
            for position, candidate in enumerate(candidates):
                conn.execute(
                    """INSERT INTO mining_candidates
                       (candidate_id, batch_revision, position, lexical_id, eligible, public_json, internal_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate["public"]["candidate_id"],
                        batch["revision_id"],
                        position,
                        candidate["lexical_id"],
                        int(candidate["public"]["eligible"]),
                        canonical_json(candidate["public"]),
                        canonical_json(candidate["internal"]),
                    ),
                )
        return self.batch_status(batch["revision_id"])

    def batch_status(self, revision_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        owns = conn is None
        connection = conn or self._connect()
        try:
            row = connection.execute("SELECT * FROM mining_batches WHERE revision_id=?", (revision_id,)).fetchone()
            require(row is not None, "batch_not_found", "Mining batch does not exist", batch_revision=revision_id)
            return {
                "schema_version": PUBLIC_SCHEMA_VERSION,
                "batch_revision": row["revision_id"],
                "state": row["state"],
                "created_at": row["created_at"],
                "profile_revision_id": row["profile_revision_id"],
                "candidate_count": row["candidate_count"],
                "eligible_count": row["eligible_count"],
                "max_cards": row["max_cards"],
                "review_pool_size": row["review_pool_size"],
                "committed_job_id": row["committed_job_id"],
            }
        finally:
            if owns:
                connection.close()

    def list_candidates(
        self,
        revision_id: str,
        *,
        offset: int,
        limit: int,
        include_ineligible: bool,
        expected_schema_version: int,
        max_payload_bytes: int,
    ) -> dict[str, Any]:
        require(
            expected_schema_version == PUBLIC_SCHEMA_VERSION,
            "unsupported_schema_version",
            "Unsupported candidate schema version",
            requested=expected_schema_version,
            supported=PUBLIC_SCHEMA_VERSION,
        )
        require(offset >= 0, "invalid_page", "offset cannot be negative")
        require(limit >= 1, "invalid_page", "limit must be positive")
        with self._connect() as conn:
            batch = conn.execute("SELECT * FROM mining_batches WHERE revision_id=?", (revision_id,)).fetchone()
            require(batch is not None, "batch_not_found", "Mining batch does not exist", batch_revision=revision_id)
            where = "batch_revision=?" + ("" if include_ineligible else " AND eligible=1")
            pool_limit = batch["review_pool_size"]
            effective_total = int(
                conn.execute(f"SELECT COUNT(*) FROM mining_candidates WHERE {where}", (revision_id,)).fetchone()[0]
            )
            if not include_ineligible and pool_limit is not None:
                effective_total = min(effective_total, int(pool_limit))
            fetch_limit = min(limit, max(0, effective_total - offset))
            rows = conn.execute(
                f"SELECT public_json FROM mining_candidates WHERE {where} ORDER BY position LIMIT ? OFFSET ?",
                (revision_id, fetch_limit, offset),
            ).fetchall()
            payload = {
                "schema_version": PUBLIC_SCHEMA_VERSION,
                "batch_revision": revision_id,
                "batch_state": batch["state"],
                "offset": offset,
                "limit": limit,
                "total": effective_total,
                "next_offset": offset + len(rows) if offset + len(rows) < effective_total else None,
                "candidates": [json.loads(row["public_json"]) for row in rows],
            }
            size = len(canonical_json(payload).encode("utf-8"))
            require(
                size <= max_payload_bytes,
                "payload_too_large",
                "Candidate page exceeds the configured response-size limit; request a smaller page",
                bytes=size,
                max_payload_bytes=max_payload_bytes,
            )
            return payload

    def get_candidates(self, revision_id: str, candidate_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(candidate_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM mining_candidates WHERE batch_revision=? AND candidate_id IN ({placeholders})",
                (revision_id, *ids),
            ).fetchall()
        by_id = {
            row["candidate_id"]: {
                "candidate_id": row["candidate_id"],
                "eligible": bool(row["eligible"]),
                "public": json.loads(row["public_json"]),
                "internal": json.loads(row["internal_json"]),
            }
            for row in rows
        }
        missing = [candidate_id for candidate_id in ids if candidate_id not in by_id]
        require(not missing, "unknown_candidate", "Selection contains unknown candidate IDs", candidate_ids=missing)
        return [by_id[candidate_id] for candidate_id in ids]

    def reserve_commit(
        self,
        revision_id: str,
        selected: list[str],
        rejected: list[str],
        metadata: dict[str, dict[str, Any]],
        enrichments: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        selection = {
            "selected": selected,
            "rejected": rejected,
            "metadata": metadata,
            "enrichments": enrichments,
        }
        job_id = content_id("job", {"batch_revision": revision_id, "selection": selection})
        with self._transaction() as conn:
            batch = conn.execute("SELECT * FROM mining_batches WHERE revision_id=?", (revision_id,)).fetchone()
            require(batch is not None, "batch_not_found", "Mining batch does not exist", batch_revision=revision_id)
            if batch["committed_job_id"] is not None:
                existing = self.job_status(batch["committed_job_id"], conn=conn)
                require(
                    batch["committed_job_id"] == job_id,
                    "batch_already_committed",
                    "This batch already has a different commit reservation",
                    job_id=batch["committed_job_id"],
                )
                return existing, False
            conn.execute(
                "INSERT INTO mining_jobs(job_id, batch_revision, state, selection_json) VALUES (?, ?, 'reserved', ?)",
                (job_id, revision_id, canonical_json(selection)),
            )
            conn.execute(
                "UPDATE mining_batches SET state='committing', committed_job_id=? WHERE revision_id=?",
                (job_id, revision_id),
            )
            selected_set = set(selected)
            rejected_set = set(rejected)
            rows = conn.execute(
                "SELECT candidate_id FROM mining_candidates WHERE batch_revision=? ORDER BY position", (revision_id,)
            ).fetchall()
            for row in rows:
                candidate_id = row["candidate_id"]
                decision = (
                    "selected"
                    if candidate_id in selected_set
                    else "rejected" if candidate_id in rejected_set else "not_reviewed"
                )
                conn.execute(
                    "INSERT INTO candidate_feedback VALUES (?, ?, ?, ?, NULL)",
                    (revision_id, candidate_id, decision, canonical_json(metadata.get(candidate_id, {}))),
                )
        return self.job_status(job_id), True

    def set_job_running(self, job_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE mining_jobs SET state='running', updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state='reserved'",
                (job_id,),
            )

    def record_output(
        self,
        job_id: str,
        candidate_id: str,
        outcome: str,
        *,
        note_id: int | None = None,
        media: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO mining_outputs(job_id, candidate_id, outcome, note_id, media_json, error_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, candidate_id) DO UPDATE SET
                     outcome=excluded.outcome, note_id=excluded.note_id,
                     media_json=excluded.media_json, error_json=excluded.error_json""",
                (
                    job_id,
                    candidate_id,
                    outcome,
                    note_id,
                    canonical_json(media or {}),
                    canonical_json(error) if error else None,
                ),
            )
            batch_revision = conn.execute(
                "SELECT batch_revision FROM mining_jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE candidate_feedback SET output_outcome=? WHERE batch_revision=? AND candidate_id=?",
                (outcome, batch_revision, candidate_id),
            )

    def finalize_job(self, job_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            totals = {
                row["outcome"]: row["n"]
                for row in conn.execute(
                    "SELECT outcome, COUNT(*) AS n FROM mining_outputs WHERE job_id=? GROUP BY outcome", (job_id,)
                )
            }
            selection = json.loads(
                conn.execute("SELECT selection_json FROM mining_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            selected_count = len(selection["selected"])
            failed = int(totals.get("failed", 0))
            finished = sum(int(value) for value in totals.values())
            if failed == 0 and finished == selected_count:
                state = "completed"
            elif selected_count > 0 and failed == selected_count:
                state = "failed"
            elif finished:
                state = "partial"
            else:
                state = "failed"
            batch_revision = conn.execute(
                "SELECT batch_revision FROM mining_jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            conn.execute("UPDATE mining_jobs SET state=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (state, job_id))
            conn.execute(
                "UPDATE mining_batches SET state=? WHERE revision_id=?",
                ("completed" if state == "completed" else "failed", batch_revision),
            )
        return self.job_status(job_id)

    def job_status(self, job_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        owns = conn is None
        connection = conn or self._connect()
        try:
            job = connection.execute("SELECT * FROM mining_jobs WHERE job_id=?", (job_id,)).fetchone()
            require(job is not None, "job_not_found", "Mining job does not exist", job_id=job_id)
            outputs = [
                {
                    "candidate_id": row["candidate_id"],
                    "outcome": row["outcome"],
                    "note_id": row["note_id"],
                    "media": json.loads(row["media_json"]),
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "review_state": json.loads(row["review_json"]),
                }
                for row in connection.execute(
                    "SELECT * FROM mining_outputs WHERE job_id=? ORDER BY candidate_id", (job_id,)
                )
            ]
            return {
                "schema_version": PUBLIC_SCHEMA_VERSION,
                "job_id": job["job_id"],
                "batch_revision": job["batch_revision"],
                "state": job["state"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "selection": json.loads(job["selection_json"]),
                "outputs": outputs,
            }
        finally:
            if owns:
                connection.close()
