"""Five-tool bounded MCP surface over the typed application facade."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from anki_miner.agent.application import AgentMiningApplication
from anki_miner.runtime import build_agent_application


def create_server(app: AgentMiningApplication) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError('The MCP server is optional. Install it with: pip install "anki-miner[mcp]"') from exc

    server = FastMCP(
        "Anki Miner",
        instructions=(
            "Use tools in this order: sync profile, prepare batch, list every candidate page, dry-run one "
            "selection, then repeat that exact selection live only after explicit user authorization. Use only "
            "eligible candidates and exact returned IDs/revisions. Use only each candidate's allowed_enrichments; "
            "Anki Miner supplies pitch, frequency, furigana, media, and all other fields."
        ),
    )

    @server.tool()
    def sync_learner_profile() -> dict[str, Any]:
        """Synchronize configured Anki knowledge sources into the local learner profile."""
        return app.sync_learner_profile()

    @server.tool()
    def prepare_mining_batch(
        inputs: list[dict[str, Any]],
        max_cards: int | None = None,
        review_pool_size: int | None = None,
    ) -> dict[str, Any]:
        """Prepare an immutable local/YouTube batch; save its returned batch_revision and max_cards."""
        return app.prepare_mining_batch(
            {"inputs": inputs, "max_cards": max_cards, "review_pool_size": review_pool_size}
        )

    @server.tool()
    def list_mining_candidates(
        batch_revision: str,
        offset: int = 0,
        limit: int | None = None,
        include_ineligible: bool = False,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        """List one candidate page; repeat with next_offset until it is null."""
        return app.list_mining_candidates(
            batch_revision,
            offset=offset,
            limit=limit,
            include_ineligible=include_ineligible,
            schema_version=schema_version,
        )

    @server.tool()
    def commit_mining_selection(
        batch_revision: str,
        candidate_ids: list[str],
        rejected_candidate_ids: list[str] | None = None,
        metadata: dict[str, dict[str, Any]] | None = None,
        enrichments: dict[str, dict[str, Any]] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Dry-run first; commit the identical selection live only with explicit user authorization."""
        return app.commit_mining_selection(
            {
                "batch_revision": batch_revision,
                "candidate_ids": candidate_ids,
                "rejected_candidate_ids": rejected_candidate_ids or [],
                "metadata": metadata or {},
                "enrichments": enrichments or {},
                "dry_run": dry_run,
            }
        )

    @server.tool()
    def get_mining_job(job_id: str) -> dict[str, Any]:
        """Inspect a durable commit job and its per-candidate receipts."""
        return app.get_mining_job(job_id)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anki-miner-mcp")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    app = None
    try:
        app = build_agent_application(args.config)
        create_server(app).run(transport="stdio")
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
