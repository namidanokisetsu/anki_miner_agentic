"""Typed application facade shared by CLI and MCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .candidates import CandidateBatchService
from .commit import MiningCommitService
from .errors import AgentMiningError
from .models import AgentProfileConfig, LocalEpisodeInput, YouTubeInput, canonical_json, content_id
from .policy import REVIEW_BATCH_BOUND
from .profile import LearnerProfileService
from .review import review_contract
from .store import AgentStore

_CANDIDATE_PAGE_SIZE = 100


class AgentMiningApplication:
    def __init__(
        self,
        store: AgentStore,
        config: AgentProfileConfig,
        profile_service: LearnerProfileService,
        candidate_service: CandidateBatchService,
        commit_service: MiningCommitService,
        *,
        mining_policy_info: dict[str, Any] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.profile_service = profile_service
        self.candidate_service = candidate_service
        self.commit_service = commit_service
        self.mining_policy_info = mining_policy_info
        self._close_callback = close_callback

    def close(self) -> None:
        if self._close_callback is not None:
            self._close_callback()
            self._close_callback = None

    def validate_learner_profile(self) -> dict[str, Any]:
        return self.profile_service.validate_mapping()

    def sync_learner_profile(self) -> dict[str, Any]:
        return self.profile_service.sync()

    def get_learner_profile(self) -> dict[str, Any]:
        result = self.store.profile_status()
        if self.mining_policy_info is not None:
            result["mining_policy"] = self.mining_policy_info
        return result

    def inspect_effective_policy(self) -> dict[str, Any]:
        """Inspect the next-run policy and live mapping health without syncing or parsing."""
        result = dict(self.mining_policy_info or {})
        try:
            mapping = self.validate_learner_profile()
        except AgentMiningError as exc:
            result["stale_mappings"] = [exc.as_dict()]
            result["ready"] = False
        else:
            result["stale_mappings"] = []
            result["mapping"] = mapping
            result["ready"] = True
        return result

    def prepare_mining_batch(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise AgentMiningError("invalid_request", "Preparation request must be an object")
        extra = sorted(set(request) - {"inputs", "max_cards"})
        if extra:
            code = "unsupported_agent_config_key" if "review_pool_size" in extra else "invalid_request"
            raise AgentMiningError(code, "Preparation request contains unsupported fields", {"fields": extra})
        parsed: list[LocalEpisodeInput | YouTubeInput] = []
        for item in request.get("inputs", []):
            if item.get("type", "local") == "local":
                parsed.append(LocalEpisodeInput.from_dict(item))
            elif item.get("type") == "youtube":
                parsed.append(YouTubeInput.from_dict(item))
            else:
                raise AgentMiningError("invalid_input", f"Unsupported input type: {item.get('type')!r}")
        return self.candidate_service.prepare(
            parsed,
            max_cards=request.get("max_cards"),
        )

    def prepare_mining_run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Synchronize, prepare, internally page, and publish one durable shortlist."""
        if not isinstance(request, dict):
            raise AgentMiningError("invalid_request", "Preparation request must be an object")
        extra = sorted(set(request) - {"inputs", "max_cards"})
        if extra:
            code = "unsupported_agent_config_key" if "review_pool_size" in extra else "invalid_request"
            raise AgentMiningError(code, "Preparation request contains unsupported fields", {"fields": extra})
        self.sync_learner_profile()
        batch = self.prepare_mining_batch(request)
        shortlist: list[dict[str, Any]] = []
        offset = 0
        page_limit = _CANDIDATE_PAGE_SIZE
        while True:
            try:
                page = self.list_mining_candidates(
                    batch["batch_revision"], offset=offset, limit=page_limit, include_ineligible=False
                )
            except AgentMiningError as exc:
                if exc.code != "payload_too_large" or page_limit == 1:
                    raise
                page_limit = max(1, page_limit // 2)
                continue
            shortlist.extend(page["candidates"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        # Keep the single response within the configured transport bound. The
        # deterministic ordering was established before definition hydration.
        required = [
            key
            for key, field in (
                ("chosen_definition", self.config.chosen_definition_field),
                ("sentence_translation", self.config.sentence_translation_field),
            )
            if field
        ]
        intended_count = len(shortlist)
        base = {
            "schema_version": 1,
            "max_cards": batch["max_cards"],
            "limits": {
                "safety_ceiling": self.config.max_cards,
                "run_ceiling": batch["max_cards"],
                "review_batch_bound": REVIEW_BATCH_BOUND,
                "semantics": "maxima_not_targets",
            },
            "required_enrichments": required,
            "review_contract": review_contract(),
            "enrichment_specs": {
                "chosen_definition": {
                    "instruction_version": "supported_one_line_definition_v1",
                    "format": "plain_text_one_line",
                    "max_chars": self.config.max_chosen_definition_chars,
                },
                "sentence_translation": {
                    "instruction_version": "close_translation_v1",
                    "format": "plain_text_one_line",
                    "max_chars": self.config.max_sentence_translation_chars,
                    "instruction": "Preserve Japanese syntax, imagery, and phrasing when understandable.",
                },
            },
            "destination": {
                "deck": self.config.write_target.deck,
                "note_type": self.config.write_target.note_type,
            },
        }
        if self.mining_policy_info is not None:
            base["mining_policy"] = self.mining_policy_info
        expected_run_id = content_id("run", {"batch_revision": batch["batch_revision"]})

        def response_for(items: list[dict[str, Any]]) -> dict[str, Any]:
            candidate_ids = [item["candidate_id"] for item in items]
            complete = len(items) == intended_count
            return base | {
                "run_id": expected_run_id,
                "shortlist": items,
                "review_batch": {
                    "candidate_ids": candidate_ids,
                    "count": len(candidate_ids),
                    "formula": "deterministically_eligible ∩ conservative_guard, ranked, up to run and review bounds",
                    "complete": complete,
                    "zero_or_shortfall_is_success": True,
                },
                "paging": {
                    "complete": complete,
                    "intended_count": intended_count,
                    "published_count": len(items),
                    "reason": None if complete else "transport_bound",
                },
            }

        while (
            shortlist and len(canonical_json(response_for(shortlist)).encode("utf-8")) > self.config.max_payload_bytes
        ):
            shortlist.pop()
        result = response_for(shortlist)
        run = self.store.create_run(batch["batch_revision"], shortlist)
        result["run_id"] = run["run_id"]
        if len(canonical_json(result).encode("utf-8")) > self.config.max_payload_bytes:
            raise AgentMiningError("payload_too_large", "The minimum run response exceeds the configured payload bound")
        return result

    def list_mining_candidates(
        self,
        batch_revision: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        include_ineligible: bool = False,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        effective_limit = _CANDIDATE_PAGE_SIZE if limit is None else limit
        if effective_limit > _CANDIDATE_PAGE_SIZE:
            raise AgentMiningError(
                "invalid_page",
                "Requested page exceeds the configured page-size limit",
                {"requested": effective_limit, "maximum": _CANDIDATE_PAGE_SIZE},
            )
        return self.store.list_candidates(
            batch_revision,
            offset=offset,
            limit=effective_limit,
            include_ineligible=include_ineligible,
            expected_schema_version=schema_version,
            max_payload_bytes=self.config.max_payload_bytes,
        )

    def commit_mining_selection(self, request: dict[str, Any]) -> dict[str, Any]:
        dry_run = request.get("dry_run", True)
        if type(dry_run) is not bool:
            raise AgentMiningError("invalid_request", "dry_run must be a boolean")
        validation_token = request.get("validation_token")
        if validation_token is not None and not isinstance(validation_token, str):
            raise AgentMiningError("invalid_request", "validation_token must be a string or null")
        return self.commit_service.commit(
            str(request.get("batch_revision", "")),
            list(request.get("candidate_ids", [])),
            rejected_candidate_ids=list(request.get("rejected_candidate_ids", [])),
            metadata=dict(request.get("metadata", {})),
            enrichments=dict(request.get("enrichments", {})),
            dry_run=dry_run,
            validation_token=validation_token,
        )

    def commit_mining_run(self, run_id: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        return self.commit_service.commit_run(run_id, reviews)

    def get_mining_job(self, job_id: str) -> dict[str, Any]:
        return self.store.job_status(job_id)
