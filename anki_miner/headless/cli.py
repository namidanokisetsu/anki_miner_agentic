"""JSON CLI for the complete agent mining workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from anki_miner.agent.application import AgentMiningApplication
from anki_miner.agent.errors import AgentMiningError
from anki_miner.exceptions import AnkiConnectionError, OperationCancelled, SetupError, SubtitleParseError
from anki_miner.runtime import build_agent_application

_HELP = {
    "settings": """settings
  agent: knowledge_sources, write_target, mature_interval_days
         safety max_cards, payload/enrichment limits and fields
  GUI profile: dictionaries, filters, word lists, ranking, media, paths and card policy
  per run: max_cards is an up-to authorization, never a target""",
    "workflow": """workflow
  prepare_mining_run -> review every returned candidate -> commit_mining_run
  Select or reject each candidate; every selection needs every required enrichment.""",
    "commands": """commands
  profile-validate | profile-sync | profile-status | policy-status
  prepare-run --request FILE | commit-run --request FILE
  Lower-level recovery: prepare, candidates, commit, job""",
}


def _help_text(topic: str | None) -> str:
    if topic:
        return _HELP[topic]
    return "help [settings|workflow|commands]\n" + "\n".join(
        f"  {name:<8} {text.splitlines()[1].strip()}" for name, text in _HELP.items()
    )


def _json_file(path: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentMiningError("malformed_json", f"Cannot read JSON request: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentMiningError("malformed_json", "JSON request must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anki-miner-agentic-agent", description="Guarded agent-first Japanese sentence mining"
    )
    parser.add_argument("--config", type=Path, help="Agent JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    help_command = commands.add_parser("help", help="Show compact agent help")
    help_command.add_argument("topic", nargs="?", choices=tuple(_HELP))
    commands.add_parser("profile-validate")
    commands.add_parser("profile-sync")
    commands.add_parser("profile-status")
    commands.add_parser("policy-status")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request", required=True, help="Preparation request JSON path, or - for stdin")
    candidates = commands.add_parser("candidates")
    candidates.add_argument("batch_revision")
    candidates.add_argument("--offset", type=int, default=0)
    candidates.add_argument("--limit", type=int)
    candidates.add_argument("--include-ineligible", action="store_true")
    candidates.add_argument("--schema-version", type=int, default=1)
    commit = commands.add_parser("commit")
    commit.add_argument("--request", required=True, help="Selection request JSON path, or - for stdin")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    run_prepare = commands.add_parser("prepare-run")
    run_prepare.add_argument("--request", required=True, help="Run preparation JSON path, or - for stdin")
    run_commit = commands.add_parser("commit-run")
    run_commit.add_argument("--request", required=True, help="Run reviews JSON path, or - for stdin")
    return parser


def _dispatch(args: argparse.Namespace, app: AgentMiningApplication) -> dict[str, Any]:
    if args.command == "profile-validate":
        return app.validate_learner_profile()
    if args.command == "profile-sync":
        return app.sync_learner_profile()
    if args.command == "profile-status":
        return app.get_learner_profile()
    if args.command == "policy-status":
        return app.inspect_effective_policy()
    if args.command == "prepare":
        return app.prepare_mining_batch(_json_file(args.request))
    if args.command == "candidates":
        return app.list_mining_candidates(
            args.batch_revision,
            offset=args.offset,
            limit=args.limit,
            include_ineligible=args.include_ineligible,
            schema_version=args.schema_version,
        )
    if args.command == "commit":
        return app.commit_mining_selection(_json_file(args.request))
    if args.command == "job":
        return app.get_mining_job(args.job_id)
    if args.command == "prepare-run":
        return app.prepare_mining_run(_json_file(args.request))
    if args.command == "commit-run":
        request = _json_file(args.request)
        return app.commit_mining_run(str(request.get("run_id", "")), list(request.get("reviews", [])))
    raise AgentMiningError("invalid_command", "Unknown command")


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, AgentMiningError):
        return 2
    if isinstance(exc, OperationCancelled):
        return 6
    if isinstance(exc, SetupError):
        return 3
    if isinstance(exc, AnkiConnectionError):
        return 4
    if isinstance(exc, SubtitleParseError):
        return 5
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        print(_help_text(args.topic))
        return 0
    if args.config is None:
        parser.error("--config is required for this command")
    app = None
    try:
        app = build_agent_application(args.config)
        result = _dispatch(args, app)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        if result.get("state") == "partial":
            return 7
        return 0
    except Exception as exc:
        payload = (
            exc.as_dict()
            if isinstance(exc, AgentMiningError)
            else {
                "code": type(exc).__name__,
                "message": str(exc),
            }
        )
        print(json.dumps({"ok": False, "error": payload}, ensure_ascii=False, sort_keys=True))
        print(f"{payload['code']}: {payload['message']}", file=sys.stderr)
        return _exit_code(exc)
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
