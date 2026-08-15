"""Typed application facade shared by CLI and MCP."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .candidates import CandidateBatchService
from .commit import MiningCommitService
from .errors import AgentMiningError
from .models import AgentProfileConfig, LocalEpisodeInput, YouTubeInput
from .profile import LearnerProfileService
from .store import AgentStore


class AgentMiningApplication:
    def __init__(
        self,
        store: AgentStore,
        config: AgentProfileConfig,
        profile_service: LearnerProfileService,
        candidate_service: CandidateBatchService,
        commit_service: MiningCommitService,
        *,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.profile_service = profile_service
        self.candidate_service = candidate_service
        self.commit_service = commit_service
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
        return self.store.profile_status()

    def prepare_mining_batch(self, request: dict[str, Any]) -> dict[str, Any]:
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
            review_pool_size=request.get("review_pool_size"),
        )

    def list_mining_candidates(
        self,
        batch_revision: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        include_ineligible: bool = False,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        effective_limit = self.config.page_size if limit is None else limit
        if effective_limit > self.config.page_size:
            raise AgentMiningError(
                "invalid_page",
                "Requested page exceeds the configured page-size limit",
                {"requested": effective_limit, "maximum": self.config.page_size},
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

    def get_mining_job(self, job_id: str) -> dict[str, Any]:
        return self.store.job_status(job_id)
