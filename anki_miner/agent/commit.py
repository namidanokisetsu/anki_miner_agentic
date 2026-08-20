"""Guarded selected-only commit, durable receipts, and retry behavior."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from anki_miner.exceptions import AnkiConnectionError, SetupError
from anki_miner.models import TokenizedWord

from .candidates import file_fingerprint
from .errors import AgentMiningError, require
from .models import AgentProfileConfig
from .policy import MAX_CHOSEN_DEFINITION_CHARS, MAX_RATIONALE_CHARS, MAX_SENTENCE_TRANSLATION_CHARS
from .review import REJECT_REASONS, REVIEW_SPEC_VERSION, SELECT_REASON
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

    def preflight(self) -> None:
        self.processor._reset_run_write_state()
        self.processor._preflight_card_target()

    @property
    def configured_tags(self) -> list[str]:
        return str(self.processor.config.anki_tags).split()

    def create_batch(self, candidates: list[dict[str, Any]], tags: list[str]) -> list[dict[str, Any]]:
        from anki_miner.orchestration.episode_processor import _EpisodeContext

        if not candidates:
            return []
        first = candidates[0]["internal"]
        video_file = Path(first["video_fingerprint"]["path"])
        words: list[TokenizedWord] = []
        for candidate in candidates:
            stored_word = dict(candidate["internal"]["word"])
            stored_word.pop("mined_form", None)
            word_fields = TokenizedWord.__dataclass_fields__
            kwargs = {name: value for name, value in stored_word.items() if name in word_fields}
            kwargs["video_file"] = video_file
            kwargs["sentence_candidates"] = []
            words.append(TokenizedWord(**kwargs))
        temp_folder = Path(tempfile.mkdtemp(prefix="anki_miner_agent_"))
        ctx = _EpisodeContext(
            start_time=time.time(),
            video_file_str=str(video_file),
            subtitle_file_str=first["subtitle_fingerprint"]["path"],
            episode_name=first["episode_id"],
            series_name="Agent Mining",
            source_label=first["episode_id"],
        )
        old_processor_config = self.processor.config
        old_anki_config = self.processor.anki_service.config
        merged_tags = list(dict.fromkeys([*old_processor_config.anki_tags.split(), *tags]))
        tagged_config = replace(old_processor_config, anki_tags=" ".join(merged_tags))
        self.processor.config = tagged_config
        self.processor.anki_service.config = tagged_config
        try:
            audio_policy = first.get("audio_track", "japanese")
            media_results = self.processor._phase3_extract(
                ctx,
                video_file,
                words,
                None,
                temp_folder,
                audio_track_override=audio_policy if type(audio_policy) is int else None,
            )
            definitions, glossaries, pitch = self.processor._phase4_lookup(ctx, media_results, None)
            word_indexes = {id(word): index for index, word in enumerate(words)}
            media_original_indexes = [word_indexes[id(word)] for word, _media in media_results]
            valid_indexes = [index for index, definition in enumerate(definitions) if definition]
            results: list[dict[str, Any]] = [
                {
                    "outcome": "failed",
                    "error": {"code": "media_or_definition_missing", "message": "Media or definition was unavailable"},
                }
                for _ in candidates
            ]
            if valid_indexes:
                filtered_media = [media_results[index] for index in valid_indexes]
                self.processor._phase5_create(
                    ctx,
                    filtered_media,
                    [definitions[index] for index in valid_indexes],
                    [glossaries[index] for index in valid_indexes],
                    [pitch[index] for index in valid_indexes],
                    None,
                    card_extra_fields=[
                        candidates[media_original_indexes[index]].get("enrichment", {}) for index in valid_indexes
                    ],
                )
                aligned = getattr(self.processor.anki_service, "last_candidate_outcomes", None)
                if aligned is None:
                    # Compatibility seam for third-party/fake processors. A
                    # single-item batch remains unambiguous.
                    if len(valid_indexes) != 1:
                        raise AgentMiningError(
                            "unaligned_anki_result", "The Anki writer did not return per-candidate outcomes"
                        )
                    note_ids = self.processor.anki_service.last_created_note_ids
                    aligned = [
                        {"outcome": "created", "note_id": note_ids[0]}
                        if note_ids
                        else {"outcome": "duplicate_skipped", "note_id": None}
                    ]
                for media_index, outcome in zip(valid_indexes, aligned, strict=True):
                    original_index = media_original_indexes[media_index]
                    media = media_results[media_index][1]
                    results[original_index] = {
                        **outcome,
                        "media": {
                            "audio": _media_fingerprint(media.audio_path),
                            "screenshot": _media_fingerprint(media.screenshot_path),
                            "expression_audio": _media_fingerprint(media.expression_audio_path),
                        },
                    }
            return results
        finally:
            self.processor.config = old_processor_config
            self.processor.anki_service.config = old_anki_config
            shutil.rmtree(temp_folder, ignore_errors=True)

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

    def commit_run(self, run_id: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        require(isinstance(reviews, list), "invalid_review", "reviews must be an array")
        candidate_ids: list[str] = []
        rejected_ids: list[str] = []
        reviewed_ids: list[str] = []
        metadata: dict[str, dict[str, Any]] = {}
        enrichments: dict[str, dict[str, Any]] = {}
        for item in reviews:
            require(isinstance(item, dict), "invalid_review", "Each review must be an object")
            extra = sorted(
                set(item)
                - {"candidate_id", "decision", "definition_option_id", "reason_code", "rationale", "enrichments"}
            )
            require(not extra, "invalid_review", "Review contains unsupported fields", fields=extra)
            candidate_id = item.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise AgentMiningError("invalid_review", "candidate_id is required")
            reviewed_ids.append(candidate_id)
            decision = item.get("decision")
            require(decision in {"select", "reject"}, "invalid_review", "decision must be select or reject")
            if decision == "select":
                candidate_ids.append(candidate_id)
                if "enrichments" in item:
                    enrichments[candidate_id] = item["enrichments"]
            else:
                rejected_ids.append(candidate_id)
                require("enrichments" not in item, "invalid_review", "Rejected candidates cannot contain enrichments")
            rationale = item.get("rationale", "")
            require(isinstance(rationale, str), "invalid_review", "rationale must be a string")
            require(
                len(rationale) <= MAX_RATIONALE_CHARS,
                "feedback_too_large",
                "rationale exceeds the fixed size limit",
                candidate_id=candidate_id,
                max_chars=MAX_RATIONALE_CHARS,
            )
            metadata[candidate_id] = {
                "review": {
                    "version": REVIEW_SPEC_VERSION,
                    "decision": decision,
                    "definition_option_id": item.get("definition_option_id"),
                    "reason_code": item.get("reason_code"),
                },
                **({"rationale": rationale} if rationale else {}),
            }
        require(
            len(reviewed_ids) == len(set(reviewed_ids)), "duplicate_review", "Reviews contain duplicate candidate IDs"
        )
        run = self.store.run_status(run_id)
        require(
            len(candidate_ids) <= run["max_cards"],
            "max_cards_exceeded",
            "Selection exceeds the user-authorized maximum",
            selected=len(candidate_ids),
            max_cards=run["max_cards"],
        )
        shortlist_ids = {item["candidate_id"] for item in run["shortlist"]}
        require(
            set(reviewed_ids) <= shortlist_ids,
            "candidate_not_in_run",
            "Reviews contain a candidate outside this run",
            candidate_ids=sorted(set(reviewed_ids) - shortlist_ids),
        )
        require(
            set(reviewed_ids) == shortlist_ids,
            "missing_candidate_reviews",
            "Every candidate in the returned review batch must be reviewed",
            candidate_ids=sorted(shortlist_ids - set(reviewed_ids)),
        )
        reviewed_rows = self.store.get_candidates(run["batch_revision"], reviewed_ids)
        selected_ids = set(candidate_ids)
        selected_rows = [row for row in reviewed_rows if row["candidate_id"] in selected_ids]
        require(
            all(row["eligible"] for row in selected_rows),
            "ineligible_selection",
            "Selection contains an ineligible candidate",
        )
        self._validate_reviews(reviewed_rows, metadata, enrichments)
        self._validate_enrichments(set(candidate_ids), enrichments, require_mapped=True)
        self._validate_sources_once(selected_rows)
        if candidate_ids:
            require(
                self.config.write_target.enabled,
                "writes_disabled",
                "Autonomous Anki writes are disabled for this profile",
            )

        job, created = self.store.reserve_run_commit(run_id, candidate_ids, rejected_ids, metadata, enrichments)
        if not created and job["state"] == "completed":
            return self._receipt(run_id, job, enrichments)
        if not candidate_ids:
            self.store.set_job_running(job["job_id"])
            return self._receipt(run_id, self.store.finalize_job(job["job_id"]), enrichments)
        # The reservation exists before any operation that can lead into the
        # write pipeline. Preflight is read-only and runs once for work that
        # actually needs to start or resume.
        preflight = getattr(self.writer, "preflight", None)
        if callable(preflight):
            preflight()
        completed = {output["candidate_id"] for output in job["outputs"] if output["outcome"] != "failed"}
        remaining = [row for row in selected_rows if row["candidate_id"] not in completed]
        self.store.set_job_running(job["job_id"])
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in remaining:
            internal = row["internal"]
            key = (
                internal["video_fingerprint"]["path"],
                internal["subtitle_fingerprint"]["path"],
                str(internal.get("audio_track", "japanese")),
            )
            groups[key].append({**row, "enrichment": enrichments.get(row["candidate_id"], {})})
        group_list = list(groups.values())
        global_error: dict[str, Any] | None = None
        for group_index, group in enumerate(group_list):
            try:
                create_batch = getattr(self.writer, "create_batch", None)
                if callable(create_batch):
                    results = create_batch(group, job["tags"])
                else:
                    results = [self.writer.create(candidate) for candidate in group]
                require(
                    len(results) == len(group),
                    "unaligned_writer_result",
                    "Writer outcomes must align with submitted candidates",
                )
                for candidate, result in zip(group, results, strict=True):
                    self.store.record_output(
                        job["job_id"],
                        candidate["candidate_id"],
                        result["outcome"],
                        note_id=result.get("note_id"),
                        media=result.get("media"),
                        error=result.get("error"),
                    )
            except (AnkiConnectionError, SetupError) as exc:
                global_error = {"code": type(exc).__name__, "message": str(exc), "global": True}
            except AgentMiningError as exc:
                global_error = exc.as_dict() | {"global": True}
            except Exception as exc:
                global_error = {
                    "code": "commit_failed",
                    "message": str(exc),
                    "type": type(exc).__name__,
                    "global": True,
                }
            if global_error is not None:
                for pending_group in group_list[group_index:]:
                    for candidate in pending_group:
                        self.store.record_output(job["job_id"], candidate["candidate_id"], "failed", error=global_error)
                break
        final = self.store.finalize_job(job["job_id"])
        return self._receipt(run_id, final, enrichments)

    def _receipt(self, run_id: str, job: dict[str, Any], enrichments: dict[str, dict[str, Any]]) -> dict[str, Any]:
        selected = job["selection"]["selected"]
        coverage = {
            key: sum(1 for candidate_id in selected if enrichments.get(candidate_id, {}).get(key))
            for key in ("chosen_definition", "sentence_translation")
            if getattr(self.config, f"{key}_field")
        }
        configured_tags = list(getattr(self.writer, "configured_tags", []))
        applied_tags = list(dict.fromkeys([*configured_tags, *job.get("tags", [])]))
        return {
            **job,
            "run_id": run_id,
            "destination": {
                "deck": self.config.write_target.deck,
                "note_type": self.config.write_target.note_type,
            },
            "enrichment_coverage": coverage,
            "review_counts": {
                "reviewed": len(selected) + len(job["selection"].get("rejected", [])),
                "selected": len(selected),
                "rejected": len(job["selection"].get("rejected", [])),
            },
            "tags": applied_tags,
            "shortfall": max(0, self.store.run_status(run_id)["max_cards"] - len(selected)),
        }

    def commit(
        self,
        batch_revision: str,
        candidate_ids: list[str],
        *,
        rejected_candidate_ids: list[str] | None = None,
        metadata: dict[str, dict[str, Any]] | None = None,
        enrichments: dict[str, dict[str, Any]] | None = None,
        dry_run: bool = True,
        validation_token: str | None = None,
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
        self._validate_enrichments(set(candidate_ids), enrichments)
        for candidate in selected_rows:
            self._validate_source(candidate)
        validation = {
            "valid": True,
            "batch_revision": batch_revision,
            "selected_count": len(candidate_ids),
            "enriched_count": len(enrichments),
            "max_cards": batch["max_cards"],
        }
        selection = {
            "selected": candidate_ids,
            "rejected": rejected,
            "metadata": metadata,
            "enrichments": enrichments,
        }
        if dry_run:
            token = self.store.record_validated_selection(batch_revision, selection)
            return validation | {"dry_run": True, "validation_token": token}
        require(
            self.config.write_target.enabled,
            "writes_disabled",
            "Autonomous Anki writes are disabled for this profile; validate with dry_run or enable the write target",
        )
        require(
            isinstance(validation_token, str) and bool(validation_token),
            "dry_run_required",
            "Live commits require a validation token returned by a successful dry run",
        )
        assert isinstance(validation_token, str)
        job, created = self.store.reserve_commit(
            batch_revision,
            candidate_ids,
            rejected,
            metadata,
            enrichments,
            validation_token,
        )
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
            extra = sorted(set(value) - {"score", "rationale", "rejection_reason", "judgment"})
            require(not extra, "invalid_feedback", "Feedback metadata contains unsupported fields", fields=extra)
            rationale = value.get("rationale", "")
            require(isinstance(rationale, str), "invalid_feedback", "rationale must be a string")
            require(
                len(rationale) <= MAX_RATIONALE_CHARS,
                "feedback_too_large",
                "rationale exceeds the fixed size limit",
                candidate_id=candidate_id,
                max_chars=MAX_RATIONALE_CHARS,
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

    def _validate_reviews(
        self,
        reviewed_rows: list[dict[str, Any]],
        metadata: dict[str, dict[str, Any]],
        enrichments: dict[str, dict[str, Any]],
    ) -> None:
        for row in reviewed_rows:
            candidate_id = row["candidate_id"]
            review = metadata.get(candidate_id, {}).get("review")
            require(
                isinstance(review, dict),
                "missing_required_review",
                "Every reviewed candidate requires a decision record",
                candidate_id=candidate_id,
            )
            assert isinstance(review, dict)
            require(
                row["public"].get("review", {}).get("contract_version") == REVIEW_SPEC_VERSION,
                "unsupported_review_version",
                "This run was prepared with a different review contract version",
                candidate_id=candidate_id,
            )
            required = {"version", "decision", "definition_option_id", "reason_code"}
            missing = sorted(required - set(review))
            extra = sorted(set(review) - required)
            require(
                not missing,
                "missing_required_review",
                "Review decision is incomplete",
                candidate_id=candidate_id,
                fields=missing,
            )
            require(
                not extra,
                "invalid_review",
                "Review contains unsupported fields",
                candidate_id=candidate_id,
                fields=extra,
            )
            require(
                review["version"] == REVIEW_SPEC_VERSION,
                "unsupported_review_version",
                "Review contract version does not match this run",
                candidate_id=candidate_id,
            )
            decision = review["decision"]
            reason_code = review["reason_code"]
            option_id = review["definition_option_id"]
            require(
                option_id is None or isinstance(option_id, str),
                "invalid_review",
                "definition_option_id must be a string or null",
                candidate_id=candidate_id,
            )
            options = row["public"].get("definition_options", [])
            allowed_ids = {option.get("option_id") for option in options}
            if decision == "select":
                require(
                    reason_code == SELECT_REASON,
                    "invalid_review",
                    "Selected candidates require the clear-supported-target reason",
                    candidate_id=candidate_id,
                )
                require(
                    option_id in allowed_ids,
                    "unsupported_definition_review",
                    "definition_option_id must identify a prepared definition option",
                    candidate_id=candidate_id,
                    option_ids=sorted(value for value in allowed_ids if value),
                )
            else:
                require(
                    reason_code in REJECT_REASONS,
                    "invalid_review",
                    "Rejected candidates require one allowed rejection reason",
                    candidate_id=candidate_id,
                    reason_codes=sorted(REJECT_REASONS),
                )
                require(
                    option_id is None,
                    "invalid_review",
                    "Rejected candidates must not select a definition option",
                    candidate_id=candidate_id,
                )

    def _validate_enrichments(
        self,
        selected_ids: set[str],
        enrichments: dict[str, dict[str, Any]],
        *,
        require_mapped: bool = False,
    ) -> None:
        require(isinstance(enrichments, dict), "invalid_enrichment", "enrichments must be an object")
        unknown = sorted(set(enrichments) - selected_ids)
        require(
            not unknown,
            "unknown_enrichment",
            "Enrichments may reference selected candidates only",
            candidate_ids=unknown,
        )
        limits = {
            "chosen_definition": (
                self.config.chosen_definition_field,
                MAX_CHOSEN_DEFINITION_CHARS,
            ),
            "sentence_translation": (
                self.config.sentence_translation_field,
                MAX_SENTENCE_TRANSLATION_CHARS,
            ),
        }
        if require_mapped:
            required = {key for key, (field, _limit) in limits.items() if field}
            if required:
                for candidate_id in selected_ids:
                    fields = enrichments.get(candidate_id)
                    require(
                        isinstance(fields, dict) and required <= set(fields),
                        "missing_required_enrichment",
                        "Every selected candidate must include all mapped enrichments",
                        candidate_id=candidate_id,
                        fields=sorted(required - set(fields or {})),
                    )
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
                    f"{key} exceeds the fixed size limit",
                    candidate_id=candidate_id,
                    max_chars=max_chars,
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

    @staticmethod
    def _validate_sources_once(candidates: list[dict[str, Any]]) -> None:
        checked: dict[Path, dict[str, Any]] = {}
        for candidate in candidates:
            for key in ("video_fingerprint", "subtitle_fingerprint"):
                stored = candidate["internal"][key]
                path = Path(stored["path"])
                if path not in checked:
                    try:
                        checked[path] = file_fingerprint(path)
                    except AgentMiningError as exc:
                        raise AgentMiningError(
                            "stale_source",
                            "A prepared source file changed or was deleted",
                            {"path": stored["path"]},
                        ) from exc
                require(
                    checked[path] == stored,
                    "stale_source",
                    "A prepared source file changed or was deleted",
                    path=stored["path"],
                )
