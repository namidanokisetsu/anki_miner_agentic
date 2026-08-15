"""Guarded selected-only commit, durable receipts, and retry behavior."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from anki_miner.models import TokenizedWord

from .candidates import file_fingerprint
from .errors import AgentMiningError, require
from .models import AgentProfileConfig
from .store import AgentStore


class CandidateWriter(Protocol):
    def create(self, candidate: dict[str, Any]) -> dict[str, Any]: ...


def _media_fingerprint(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"filename": path.name, "sha256": digest, "bytes": path.stat().st_size}


class ExistingPipelineCandidateWriter:
    """Reuse the established media, lookup, note-builder, and Anki stages."""

    def __init__(self, processor: Any) -> None:
        self.processor = processor

    def create(self, candidate: dict[str, Any]) -> dict[str, Any]:
        from anki_miner.orchestration.episode_processor import _EpisodeContext

        internal = candidate["internal"]
        stored_word = dict(internal["word"])
        stored_word.pop("mined_form", None)
        word_fields = TokenizedWord.__dataclass_fields__
        word_kwargs = {name: value for name, value in stored_word.items() if name in word_fields}
        word_kwargs["video_file"] = Path(internal["video_fingerprint"]["path"])
        word_kwargs["sentence_candidates"] = []
        word = TokenizedWord(**word_kwargs)
        video_file = Path(internal["video_fingerprint"]["path"])
        temp_folder = Path(tempfile.mkdtemp(prefix="anki_miner_agent_"))
        ctx = _EpisodeContext(
            start_time=time.time(),
            video_file_str=str(video_file),
            subtitle_file_str=internal["subtitle_fingerprint"]["path"],
            episode_name=internal["episode_id"],
            series_name="Agent Mining",
            source_label=internal["episode_id"],
        )
        try:
            self.processor._reset_run_write_state()
            self.processor._preflight_card_target()
            audio_policy = internal.get("audio_track", "japanese")
            audio_track_override = audio_policy if type(audio_policy) is int else None
            media_results = self.processor._phase3_extract(
                ctx,
                video_file,
                [word],
                None,
                temp_folder,
                audio_track_override=audio_track_override,
            )
            definitions, glossaries, pitch = self.processor._phase4_lookup(ctx, media_results, None)
            _count, note_ids, _forms = self.processor._phase5_create(
                ctx,
                media_results,
                definitions,
                glossaries,
                pitch,
                None,
                card_extra_fields=[dict(candidate.get("enrichment", {}))],
            )
            media_receipt: dict[str, Any] = {}
            if media_results:
                media = media_results[0][1]
                media_receipt = {
                    "audio": _media_fingerprint(media.audio_path),
                    "screenshot": _media_fingerprint(media.screenshot_path),
                    "expression_audio": _media_fingerprint(media.expression_audio_path),
                }
            if note_ids:
                return {"outcome": "created", "note_id": note_ids[0], "media": media_receipt}
            if self.processor.anki_service.last_skipped_duplicates:
                return {"outcome": "duplicate_skipped", "note_id": None, "media": media_receipt}
            return {
                "outcome": "failed",
                "note_id": None,
                "media": media_receipt,
                "error": {"code": "card_not_created", "message": "No definition-backed Anki note was created"},
            }
        finally:
            shutil.rmtree(temp_folder, ignore_errors=True)


class MiningCommitService:
    def __init__(
        self,
        store: AgentStore,
        config: AgentProfileConfig,
        writer: CandidateWriter,
    ) -> None:
        self.store = store
        self.config = config
        self.writer = writer

    def commit(
        self,
        batch_revision: str,
        candidate_ids: list[str],
        *,
        rejected_candidate_ids: list[str] | None = None,
        metadata: dict[str, dict[str, Any]] | None = None,
        enrichments: dict[str, dict[str, Any]] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        rejected = rejected_candidate_ids or []
        metadata = metadata or {}
        enrichments = enrichments or {}
        require(
            len(candidate_ids) == len(set(candidate_ids)),
            "duplicate_selection",
            "Selection contains duplicate candidate IDs",
        )
        require(
            len(rejected) == len(set(rejected)),
            "duplicate_selection",
            "Rejected candidate IDs contain duplicates",
        )
        require(
            not (set(candidate_ids) & set(rejected)),
            "conflicting_feedback",
            "A candidate cannot be both selected and rejected",
        )
        candidate_ids = sorted(candidate_ids)
        rejected = sorted(rejected)
        batch = self.store.batch_status(batch_revision)
        require(
            len(candidate_ids) <= batch["max_cards"],
            "max_cards_exceeded",
            "Selection exceeds the batch-wide card limit",
            selected=len(candidate_ids),
            max_cards=batch["max_cards"],
        )
        selected_rows = self.store.get_candidates(batch_revision, candidate_ids)
        self.store.get_candidates(batch_revision, rejected)
        ineligible = [row["candidate_id"] for row in selected_rows if not row["eligible"]]
        require(
            not ineligible,
            "ineligible_selection",
            "Selection contains candidates excluded by deterministic policy",
            candidate_ids=ineligible,
        )
        self._validate_metadata(set(candidate_ids) | set(rejected), metadata)
        self._validate_enrichments(set(candidate_ids), selected_rows, enrichments)
        for candidate in selected_rows:
            self._validate_source(candidate)
        validation = {
            "valid": True,
            "batch_revision": batch_revision,
            "selected_count": len(candidate_ids),
            "enriched_count": len(enrichments),
            "max_cards": batch["max_cards"],
        }
        if dry_run:
            return validation | {"dry_run": True}
        require(
            self.config.write_target.enabled,
            "writes_disabled",
            "Autonomous Anki writes are disabled for this profile; validate with dry_run or enable the write target",
        )
        job, created = self.store.reserve_commit(batch_revision, candidate_ids, rejected, metadata, enrichments)
        if not created and job["state"] == "completed":
            return job
        completed = {output["candidate_id"] for output in job["outputs"] if output["outcome"] != "failed"}
        self.store.set_job_running(job["job_id"])
        for candidate in selected_rows:
            if candidate["candidate_id"] in completed:
                continue
            try:
                result = self.writer.create({**candidate, "enrichment": enrichments.get(candidate["candidate_id"], {})})
                self.store.record_output(
                    job["job_id"],
                    candidate["candidate_id"],
                    result["outcome"],
                    note_id=result.get("note_id"),
                    media=result.get("media"),
                    error=result.get("error"),
                )
            except Exception as exc:
                error = (
                    exc.as_dict()
                    if isinstance(exc, AgentMiningError)
                    else {
                        "code": "commit_failed",
                        "message": str(exc),
                        "type": type(exc).__name__,
                    }
                )
                self.store.record_output(job["job_id"], candidate["candidate_id"], "failed", error=error)
        return self.store.finalize_job(job["job_id"])

    def _validate_metadata(self, allowed_ids: set[str], metadata: dict[str, dict[str, Any]]) -> None:
        unknown = sorted(set(metadata) - allowed_ids)
        require(
            not unknown, "unknown_feedback", "Feedback metadata references unselected candidates", candidate_ids=unknown
        )
        for candidate_id, value in metadata.items():
            require(isinstance(value, dict), "invalid_feedback", "Feedback metadata must be an object")
            extra = sorted(set(value) - {"score", "rationale", "rejection_reason"})
            require(not extra, "invalid_feedback", "Feedback metadata contains unsupported fields", fields=extra)
            rationale = value.get("rationale", "")
            require(isinstance(rationale, str), "invalid_feedback", "rationale must be a string")
            require(
                len(rationale) <= self.config.max_rationale_chars,
                "feedback_too_large",
                "rationale exceeds the configured size limit",
                candidate_id=candidate_id,
                max_chars=self.config.max_rationale_chars,
            )
            score = value.get("score")
            require(score is None or type(score) in (int, float), "invalid_feedback", "score must be numeric")
            rejection_reason = value.get("rejection_reason", "")
            require(isinstance(rejection_reason, str), "invalid_feedback", "rejection_reason must be a string")
            require(
                len(rejection_reason) <= 100,
                "feedback_too_large",
                "rejection_reason exceeds 100 characters",
                candidate_id=candidate_id,
            )

    def _validate_enrichments(
        self,
        selected_ids: set[str],
        selected_rows: list[dict[str, Any]],
        enrichments: dict[str, dict[str, Any]],
    ) -> None:
        require(isinstance(enrichments, dict), "invalid_enrichment", "enrichments must be an object")
        unknown = sorted(set(enrichments) - selected_ids)
        require(
            not unknown,
            "unknown_enrichment",
            "Enrichments may reference selected candidates only",
            candidate_ids=unknown,
        )
        candidates = {row["candidate_id"]: row for row in selected_rows}
        limits = {
            "chosen_definition": (
                self.config.chosen_definition_field,
                self.config.max_chosen_definition_chars,
            ),
            "sentence_translation": (
                self.config.sentence_translation_field,
                self.config.max_sentence_translation_chars,
            ),
        }
        for candidate_id, fields in enrichments.items():
            require(isinstance(fields, dict), "invalid_enrichment", "Candidate enrichment must be an object")
            extra = sorted(set(fields) - set(limits))
            require(
                not extra,
                "invalid_enrichment",
                "Candidate enrichment contains unsupported fields",
                candidate_id=candidate_id,
                fields=extra,
            )
            require(bool(fields), "invalid_enrichment", "Candidate enrichment cannot be empty")
            for key, value in fields.items():
                target_field, max_chars = limits[key]
                require(
                    bool(target_field),
                    "unmapped_enrichment",
                    f"{key} has no configured Anki field",
                    candidate_id=candidate_id,
                )
                require(isinstance(value, str), "invalid_enrichment", f"{key} must be a string")
                require(
                    bool(value) and value == value.strip(),
                    "invalid_enrichment",
                    f"{key} must be non-empty with no surrounding whitespace",
                    candidate_id=candidate_id,
                )
                require(
                    not any(ord(char) < 32 or char in {"\u2028", "\u2029"} for char in value),
                    "invalid_enrichment",
                    f"{key} must be plain text on one line",
                    candidate_id=candidate_id,
                )
                require(
                    len(value) <= max_chars,
                    "enrichment_too_large",
                    f"{key} exceeds the configured size limit",
                    candidate_id=candidate_id,
                    max_chars=max_chars,
                )
            if "chosen_definition" in fields:
                require(
                    bool(candidates[candidate_id]["public"].get("definition_options")),
                    "missing_definition_options",
                    "A chosen definition requires dictionary options from the prepared batch",
                    candidate_id=candidate_id,
                )

    @staticmethod
    def _validate_source(candidate: dict[str, Any]) -> None:
        for key in ("video_fingerprint", "subtitle_fingerprint"):
            stored = candidate["internal"][key]
            current = file_fingerprint(Path(stored["path"]))
            require(
                current == stored,
                "stale_source",
                "A prepared source file changed or was deleted",
                path=stored["path"],
            )
