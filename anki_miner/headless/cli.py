"""Unified JSON CLI for agent mining and standalone media operations."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, Iterator, Literal, Never, cast

from anki_miner.agent.application import AgentMiningApplication
from anki_miner.agent.errors import AgentMiningError
from anki_miner.exceptions import (
    AlassNotFoundError,
    AnkiConnectionError,
    AnkiMinerException,
    FfmpegNotFoundError,
    OperationCancelled,
    SetupError,
    SubtitleParseError,
    YtdlpNotFoundError,
)
from anki_miner.headless.errors import HeadlessCommandError
from anki_miner.runtime import build_agent_application
from anki_miner.utils.atomic_io import atomic_write_path

_HELP = {
    "settings": """settings
  agent: knowledge_sources, write_target, mature_interval_days
         enrichment fields and audio track
  GUI profile: dictionaries, filters, media, paths and card policy
  per run: max_cards is the only count limit and is never a target""",
    "workflow": """workflow
  mine prepare -> review every returned candidate -> mine commit
  Select or reject each candidate; every selection needs every required enrichment.""",
    "commands": """commands
  probe | retime | condense | transcribe | download
  profile validate|sync|status | mine prepare|commit|job
  Compatibility: prepare-run | commit-run and lower-level recovery commands""",
}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise HeadlessCommandError("invalid_arguments", message, exit_code=2)


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


def _commit_summary(result: dict[str, Any]) -> dict[str, Any]:
    failures = [
        {key: item[key] for key in ("candidate_id", "error") if key in item}
        for item in result.get("outputs", [])
        if item.get("outcome") == "failed"
    ]
    return {
        key: result[key]
        for key in (
            "job_id",
            "state",
            "review_counts",
            "counts",
            "destination",
            "enrichment_coverage",
            "shortfall",
            "job_tag_query",
        )
        if key in result
    } | {"failures": failures}


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _add_result_output(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--output", type=Path, required=required, help="Write the complete JSON result to this file")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing JSON result file")


def _add_agent_compatibility_commands(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("profile-validate", help=argparse.SUPPRESS)
    commands.add_parser("profile-sync", help=argparse.SUPPRESS)
    commands.add_parser("profile-status", help=argparse.SUPPRESS)
    commands.add_parser("policy-status", help=argparse.SUPPRESS)
    prepare = commands.add_parser("prepare", help=argparse.SUPPRESS)
    prepare.add_argument("--request", required=True)
    candidates = commands.add_parser("candidates", help=argparse.SUPPRESS)
    candidates.add_argument("batch_revision")
    candidates.add_argument("--offset", type=int, default=0)
    candidates.add_argument("--limit", type=int)
    candidates.add_argument("--include-ineligible", action="store_true")
    candidates.add_argument("--schema-version", type=int, default=1)
    commit = commands.add_parser("commit", help=argparse.SUPPRESS)
    commit.add_argument("--request", required=True)
    job = commands.add_parser("job", help=argparse.SUPPRESS)
    job.add_argument("job_id")
    run_prepare = commands.add_parser("prepare-run", help=argparse.SUPPRESS)
    run_prepare.add_argument("--request", required=True)
    _add_result_output(run_prepare, required=False)
    run_commit = commands.add_parser("commit-run", help=argparse.SUPPRESS)
    run_commit.add_argument("--request", required=True)
    _add_result_output(run_commit, required=False)
    run_commit.add_argument("--summary", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="anki_miner", description="Anki Miner automation CLI")
    parser.add_argument("--config", type=Path, help="Agent JSON configuration (required for profile/mine commands)")
    commands = parser.add_subparsers(dest="command", required=True)

    help_command = commands.add_parser("help", help="Show compact agent help")
    help_command.add_argument("topic", nargs="?", choices=tuple(_HELP))

    probe = commands.add_parser("probe", help="Inspect media duration and tracks")
    probe.add_argument("media", type=Path)

    retime = commands.add_parser("retime", help="Retime a subtitle against media")
    retime.add_argument("--video", type=Path, required=True)
    retime.add_argument("--subtitle", type=Path, required=True)
    retime.add_argument("--output", type=Path, required=True)
    retime.add_argument("--overwrite", action="store_true")
    reference = retime.add_mutually_exclusive_group()
    reference.add_argument("--reference-subtitle-track", type=_nonnegative_int)
    reference.add_argument("--reference-audio-track", type=_nonnegative_int)

    condense = commands.add_parser("condense", help="Create dialogue-only condensed audio")
    condense.add_argument("--media", type=Path, required=True)
    condense.add_argument("--subtitle", type=Path)
    condense.add_argument("--output", type=Path, required=True)
    condense.add_argument("--overwrite", action="store_true")
    condense.add_argument("--padding-ms", type=_nonnegative_int)
    condense.add_argument("--offset-ms", type=int)
    condense.add_argument("--bitrate-kbps", type=_positive_int)
    condense.add_argument("--filtered-chars")
    condense.add_argument("--write-subtitles", action=argparse.BooleanOptionalAction, default=None)
    condense.add_argument("--audio-track", type=_nonnegative_int)
    condense.add_argument("--subtitle-track", type=_nonnegative_int)

    transcribe = commands.add_parser("transcribe", help="Generate SRT subtitles with configured ASR")
    transcribe.add_argument("--media", type=Path, required=True)
    transcribe.add_argument("--output", type=Path, required=True)
    transcribe.add_argument("--overwrite", action="store_true")
    transcribe.add_argument("--audio-track", type=_nonnegative_int)

    download = commands.add_parser("download", help="Download one URL with configured yt-dlp")
    download.add_argument("--url", required=True)
    download.add_argument("--output-dir", type=Path, required=True)
    download.add_argument("--preset", choices=("best", "1440p", "1080p", "720p", "audio_mp3", "audio_m4a"))
    download.add_argument("--format-selector")
    download.add_argument("--write-subtitles", action=argparse.BooleanOptionalAction, default=None)
    download.add_argument("--subtitle-languages")
    download.add_argument("--embed-thumbnail", action=argparse.BooleanOptionalAction, default=None)
    download.add_argument("--embed-metadata", action=argparse.BooleanOptionalAction, default=None)

    profile = commands.add_parser("profile", help="Validate, synchronize, or inspect the learner profile")
    profile.add_argument("action", choices=("validate", "sync", "status"))

    mine = commands.add_parser("mine", help="Run the guarded prepare/review/commit workflow")
    mine_commands = mine.add_subparsers(dest="mine_command", required=True)
    mine_prepare = mine_commands.add_parser("prepare")
    mine_prepare.add_argument("--request", required=True)
    _add_result_output(mine_prepare, required=True)
    mine_commit = mine_commands.add_parser("commit")
    mine_commit.add_argument("--request", required=True)
    _add_result_output(mine_commit, required=True)
    mine_commit.add_argument("--summary", action="store_true")
    mine_job = mine_commands.add_parser("job")
    mine_job.add_argument("job_id")

    _add_agent_compatibility_commands(commands)
    return parser


def _reviews_request(path: str) -> tuple[str, list[dict[str, Any]]]:
    request = _json_file(path)
    reviews = request.get("reviews")
    if not isinstance(reviews, list) or not all(isinstance(item, dict) for item in reviews):
        raise AgentMiningError("malformed_json", "reviews must be an array of objects")
    return str(request.get("run_id", "")), reviews


def _dispatch_agent(args: argparse.Namespace, app: AgentMiningApplication) -> dict[str, Any]:
    if args.command == "profile":
        if args.action == "validate":
            return app.validate_learner_profile()
        if args.action == "sync":
            return app.sync_learner_profile()
        return app.get_learner_profile()
    if args.command == "mine":
        if args.mine_command == "prepare":
            return app.prepare_mining_run(_json_file(args.request))
        if args.mine_command == "commit":
            run_id, reviews = _reviews_request(args.request)
            return app.commit_mining_run(run_id, reviews)
        return app.get_mining_job(args.job_id)
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
        run_id, reviews = _reviews_request(args.request)
        return app.commit_mining_run(run_id, reviews)
    raise HeadlessCommandError("invalid_command", "Unknown command", exit_code=2)


class _Progress:
    """Small stderr reporter that bounds noisy service callbacks."""

    def __init__(self) -> None:
        self._last_bucket: dict[str, int] = {}
        self._last_message: str | None = None

    def line(self, message: str) -> None:
        if message != self._last_message:
            print(message, file=sys.stderr, flush=True)
            self._last_message = message

    def percent(self, label: str, fraction: float | int) -> None:
        value = float(fraction)
        percent = int(value * 100) if value <= 1 else int(value)
        percent = max(0, min(100, percent))
        bucket = percent // 5
        if self._last_bucket.get(label) != bucket or percent == 100:
            self._last_bucket[label] = bucket
            self.line(f"{label}: {percent}%")

    def download(self, message: str, fraction: float | None) -> None:
        if fraction is None:
            self.line(message)
        else:
            self.percent(message, fraction)


@contextmanager
def _cooperative_cancellation() -> Iterator[threading.Event]:
    event = threading.Event()
    installed: dict[signal.Signals, Any] = {}

    def cancel(_signum: int, _frame: FrameType | None) -> None:
        if not event.is_set():
            print("Cancellation requested; stopping safely...", file=sys.stderr, flush=True)
        event.set()

    if threading.current_thread() is threading.main_thread():
        for signame in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signame, None)
            if signum is not None:
                installed[signum] = signal.getsignal(signum)
                signal.signal(signum, cancel)
    try:
        yield event
    finally:
        for signum, handler in installed.items():
            signal.signal(signum, handler)


def _dispatch_media(args: argparse.Namespace) -> dict[str, Any]:
    from anki_miner.headless.media_commands import (
        condense_media,
        download_media,
        load_active_config,
        probe_media,
        retime_media_subtitle,
        transcribe_media,
    )

    config = load_active_config()
    if args.command == "probe":
        return probe_media(config, args.media)
    progress = _Progress()
    with _cooperative_cancellation() as cancel_event:
        if args.command == "retime":
            reference_kind: Literal["subtitle", "audio"] | None
            if args.reference_subtitle_track is not None:
                reference_kind, reference_index = "subtitle", args.reference_subtitle_track
            elif args.reference_audio_track is not None:
                reference_kind, reference_index = "audio", args.reference_audio_track
            else:
                reference_kind, reference_index = None, None
            return retime_media_subtitle(
                config,
                args.video,
                args.subtitle,
                args.output,
                overwrite=args.overwrite,
                reference_kind=reference_kind,
                reference_index=reference_index,
                cancel_event=cancel_event,
                log_cb=progress.line,
            )
        if args.command == "condense":
            return condense_media(
                config,
                args.media,
                args.subtitle,
                args.output,
                overwrite=args.overwrite,
                padding_ms=config.condenser_padding_ms if args.padding_ms is None else args.padding_ms,
                offset_ms=config.condenser_offset_ms if args.offset_ms is None else args.offset_ms,
                bitrate_kbps=config.condenser_bitrate_kbps if args.bitrate_kbps is None else args.bitrate_kbps,
                filtered_chars=config.condenser_filtered_chars if args.filtered_chars is None else args.filtered_chars,
                write_subtitles=(
                    config.condenser_write_subtitles if args.write_subtitles is None else args.write_subtitles
                ),
                audio_track=args.audio_track,
                subtitle_track=args.subtitle_track,
                cancel_event=cancel_event,
                progress_cb=lambda percent: progress.percent("Condensing", percent),
            )
        if args.command == "transcribe":
            return transcribe_media(
                config,
                args.media,
                args.output,
                overwrite=args.overwrite,
                audio_track=args.audio_track,
                cancel_event=cancel_event,
                progress_cb=lambda fraction: progress.percent("Transcribing", fraction),
            )
        if args.command == "download":
            return download_media(
                config,
                args.url,
                args.output_dir,
                preset=args.preset,
                format_selector=args.format_selector,
                write_subtitles=args.write_subtitles,
                subtitle_languages=args.subtitle_languages,
                embed_thumbnail=args.embed_thumbnail,
                embed_metadata=args.embed_metadata,
                cancel_event=cancel_event,
                progress_cb=progress.download,
            )
    raise HeadlessCommandError("invalid_command", "Unknown command", exit_code=2)


def _is_agent_command(args: argparse.Namespace) -> bool:
    return args.command in {
        "profile",
        "mine",
        "profile-validate",
        "profile-sync",
        "profile-status",
        "policy-status",
        "prepare",
        "candidates",
        "commit",
        "job",
        "prepare-run",
        "commit-run",
    }


def _result_output(args: argparse.Namespace) -> Path | None:
    if args.command in {"prepare-run", "commit-run"}:
        return cast(Path | None, args.output)
    if args.command == "mine" and args.mine_command in {"prepare", "commit"}:
        return cast(Path | None, args.output)
    return None


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool, request: str | None) -> None:
    destination = path.expanduser().resolve()
    request_path = None if request in (None, "-") else Path(request).expanduser().resolve()
    if request_path == destination:
        raise HeadlessCommandError(
            "unsafe_output",
            "JSON output must be distinct from the request file",
            exit_code=2,
            details={"path": str(destination)},
        )
    if destination.exists() and not overwrite:
        raise HeadlessCommandError(
            "output_exists",
            "JSON output already exists; pass --overwrite to replace it",
            exit_code=2,
            details={"path": str(destination)},
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with atomic_write_path(destination) as staged:
        staged.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _preflight_json_output(args: argparse.Namespace) -> None:
    """Refuse an unsafe receipt path before prepare/commit can do any work."""
    output = _result_output(args)
    if output is None:
        return
    destination = output.expanduser().resolve()
    request = getattr(args, "request", None)
    request_path = None if request in (None, "-") else Path(request).expanduser().resolve()
    if destination == request_path:
        raise HeadlessCommandError(
            "unsafe_output",
            "JSON output must be distinct from the request file",
            exit_code=2,
            details={"path": str(destination)},
        )
    if destination.exists() and not getattr(args, "overwrite", False):
        raise HeadlessCommandError(
            "output_exists",
            "JSON output already exists; pass --overwrite to replace it",
            exit_code=2,
            details={"path": str(destination)},
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HeadlessCommandError(
            "invalid_output",
            f"Cannot create the JSON output directory: {exc}",
            exit_code=2,
            details={"path": str(destination.parent)},
        ) from exc


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, HeadlessCommandError):
        return exc.exit_code
    if isinstance(exc, AgentMiningError):
        return 2
    if isinstance(exc, OperationCancelled):
        return 6
    if isinstance(exc, (YtdlpNotFoundError, FfmpegNotFoundError, AlassNotFoundError, SetupError)):
        return 3
    if isinstance(exc, AnkiConnectionError):
        return 4
    if isinstance(exc, (SubtitleParseError, AnkiMinerException)):
        return 5
    return 1


def _error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, (HeadlessCommandError, AgentMiningError)):
        return exc.as_dict()
    return {"code": type(exc).__name__, "message": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    app = None
    try:
        args = parser.parse_args(argv)
        if args.command == "help":
            print(_help_text(args.topic))
            return 0
        if _is_agent_command(args):
            if args.config is None:
                raise HeadlessCommandError(
                    "missing_agent_config", "--config is required for profile and mine commands", exit_code=2
                )
            _preflight_json_output(args)
            app = build_agent_application(args.config)
            result = _dispatch_agent(args, app)
        else:
            result = _dispatch_media(args)

        payload = {"ok": True, "result": result}
        output_path = _result_output(args)
        if output_path is not None:
            _write_json(
                output_path,
                payload,
                overwrite=getattr(args, "overwrite", False),
                request=getattr(args, "request", None),
            )
        if getattr(args, "summary", False):
            print(json.dumps({"ok": True, "result": _commit_summary(result)}, ensure_ascii=False, sort_keys=True))
        elif output_path is None:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 7 if result.get("state") == "partial" else 0
    except KeyboardInterrupt:
        exc = HeadlessCommandError("cancelled", "Operation cancelled", exit_code=6)
        payload = exc.as_dict()
        print(json.dumps({"ok": False, "error": payload}, ensure_ascii=False, sort_keys=True))
        print(f"{payload['code']}: {payload['message']}", file=sys.stderr)
        return 6
    except Exception as exc:
        payload = _error_payload(exc)
        print(json.dumps({"ok": False, "error": payload}, ensure_ascii=False, sort_keys=True))
        print(f"{payload['code']}: {payload['message']}", file=sys.stderr)
        return _exit_code(exc)
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
