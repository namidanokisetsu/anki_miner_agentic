"""Two-call bounded MCP surface over the typed application facade."""

import argparse
import sys
from pathlib import Path
from typing import Any

from anki_miner.agent.application import AgentMiningApplication
from anki_miner.runtime import build_agent_application


def create_server(app: AgentMiningApplication) -> Any:
    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            'The MCP server is optional. Install it with: pip install "anki-miner-agentic[mcp]"'
        ) from exc

    server = FastMCP(
        "Anki Miner Agentic",
        instructions=(
            "Use prepare_mining_run once, choose no more than its max_cards from the returned shortlist, provide "
            "every required enrichment for every selection, then call commit_mining_run once. The user's stated "
            "maximum authorizes that write. Anki Miner supplies pitch, frequency, furigana, media, and other fields."
        ),
    )

    @server.tool()
    async def prepare_mining_run(
        inputs: list[dict[str, Any]],
        max_cards: int,
        ctx: Context,
        review_pool_size: int | None = None,
    ) -> dict[str, Any]:
        """Synchronize and return one durable, bounded candidate shortlist."""
        await ctx.report_progress(0, 2, "Synchronizing profile and preparing shortlist")
        result = app.prepare_mining_run(
            {"inputs": inputs, "max_cards": max_cards, "review_pool_size": review_pool_size}
        )
        await ctx.report_progress(2, 2, "Mining run prepared")
        return result

    @server.tool()
    async def commit_mining_run(
        run_id: str,
        selections: list[dict[str, Any]],
        ctx: Context,
    ) -> dict[str, Any]:
        """Validate and synchronously commit an enriched selection; unchanged retries are idempotent."""
        await ctx.report_progress(0, 2, "Validating selection and processing source groups")
        result = app.commit_mining_run(run_id, selections)
        await ctx.report_progress(2, 2, "Terminal mining receipt ready")
        return result

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anki-miner-agentic-mcp")
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
