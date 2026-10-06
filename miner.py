"""Prepare a shortlist, then submit selected words to upstream Anki Miner."""

import argparse
import hashlib
import json
import math
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from candidates import discover
from transport import known_words, read_json, require_success, upstream, write_json


def require(condition, message):
    if not condition:
        raise ValueError(message)


def object_keys(value, allowed, required=()):
    require(isinstance(value, dict), "Expected a JSON object")
    require(not set(value) - set(allowed), f"Unknown keys: {sorted(set(value) - set(allowed))}")
    require(set(required) <= set(value), f"Required keys: {sorted(required)}")


def load_config(path):
    raw = read_json(path)
    object_keys(raw, {"upstream", "profile", "language", "anki_connect", "anki_key_env", "known_words"})
    config = {
        "upstream": ["anki-miner"],
        "profile": None,
        "language": "ja",
        "anki_connect": "http://127.0.0.1:8765",
        **raw,
    }
    command = config["upstream"]
    require(
        isinstance(command, list) and command and all(isinstance(x, str) and x for x in command),
        'upstream must be a nonempty command array, e.g. ["C:/path/AnkiMiner.exe"]',
    )
    require(config["language"] == "ja", "Candidate discovery currently supports Japanese (ja)")
    require(
        config["profile"] is None or isinstance(config["profile"], str), "profile must be a string or null"
    )
    require(isinstance(config["anki_connect"], str), "anki_connect must be a URL string")
    url = urlparse(config["anki_connect"])
    require(
        url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"},
        "anki_connect must be a local HTTP endpoint",
    )
    if "anki_key_env" in config:
        require(
            isinstance(config["anki_key_env"], str) and config["anki_key_env"],
            "anki_key_env must name an environment variable",
        )
    if "known_words" in config:
        require(isinstance(config["known_words"], list), "known_words must be a list")
        for source in config["known_words"]:
            object_keys(source, {"query", "field"}, {"query", "field"})
            require(
                all(isinstance(value, str) and value.strip() for value in source.values()),
                "Each knowledge source needs a nonempty Anki query and field name",
            )
    return config


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def fingerprint(path):
    path = Path(path).expanduser().resolve(strict=True)
    require(path.is_file(), f"Not a file: {path}")
    before = path.stat()
    with path.open("rb") as handle:
        content_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    after = path.stat()
    require(
        (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
        f"Source changed while reading: {path}",
    )
    return {"path": str(path), "size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": content_hash}


def profile_args(config):
    return ["--profile", config["profile"]] if config["profile"] is not None else []


def doctor(config):
    with tempfile.TemporaryDirectory(prefix="anki-lean-check-") as temporary:
        log = Path(temporary) / "upstream.log"
        version = require_success(upstream(config, "version", log=log))
        require(
            version.get("schema") == 1 and "mine" in version.get("commands", []),
            "This executable does not support upstream's schema-1 mining API",
        )
        checks = require_success(
            upstream(config, "check", "--language", "ja", *profile_args(config), log=log)
        )
        return {
            "ok": bool(checks.get("ready")),
            "upstream_version": version.get("app"),
            "checks": checks.get("items", []),
        }


def parse_sources(request):
    object_keys(request, {"inputs", "max_cards"}, {"inputs", "max_cards"})
    require(
        type(request["max_cards"]) is int and 1 <= request["max_cards"] <= 1000,
        "max_cards must be an explicitly authorized integer from 1 to 1000",
    )
    require(
        isinstance(request["inputs"], list) and 1 <= len(request["inputs"]) <= 50,
        "inputs must contain 1 to 50 local video/subtitle pairs",
    )
    sources, hashes = [], {}
    for item in request["inputs"]:
        object_keys(
            item,
            {"video_file", "subtitle_file", "subtitle_offset", "audio_track_override"},
            {"video_file", "subtitle_file"},
        )
        source = {}
        for key in ("video_file", "subtitle_file"):
            require(isinstance(item[key], str) and item[key], f"{key} must be a path")
            path = Path(item[key]).expanduser().resolve(strict=True)
            if path not in hashes:
                hashes[path] = fingerprint(path)
            source[key] = str(path)
            source[key + "_fingerprint"] = hashes[path]
        offset = item.get("subtitle_offset", 0)
        require(type(offset) in (int, float) and math.isfinite(offset), "subtitle_offset must be finite")
        audio = item.get("audio_track_override")
        require(
            audio is None or type(audio) is int and audio >= 0, "audio_track_override must be nonnegative"
        )
        source.update(subtitle_offset=offset, audio_track_override=audio)
        sources.append(source)
    return sources


def prepare(config, request, folder):
    folder = Path(folder).expanduser().resolve()
    require(not folder.exists(), "Use a new run folder; prepared runs are immutable")
    sources = parse_sources(request)
    # Finish all read-only work before publishing the run directory.
    with tempfile.TemporaryDirectory(prefix="anki-lean-prepare-") as temporary:
        settings_file = Path(temporary) / "settings.json"
        log = Path(temporary) / "upstream.log"
        checks = require_success(
            upstream(config, "check", "--language", "ja", *profile_args(config), log=log)
        )
        require(
            checks.get("ready") is True,
            "Upstream setup is incomplete; run doctor to see the checks that failed",
        )
        require_success(
            upstream(
                config,
                "settings-export",
                "--language",
                "ja",
                "--out",
                settings_file,
                *profile_args(config),
                log=log,
            )
        )
        exported = read_json(settings_file)
        require(
            exported.get("anki_miner_settings") == 1 and exported.get("configured", True),
            "Set up the Japanese profile in upstream Anki Miner first",
        )
        settings = exported["settings"]
        target = {key: settings[key] for key in ("anki_deck_name", "anki_note_type", "anki_fields")}
        for key in ("card_type", "card_type_marker_fields"):
            if key in settings:
                target[key] = settings[key]
        target["allow_duplicate_cards"] = False
        field = target["anki_fields"]["word"]
        require(isinstance(field, str) and field, "Upstream must have an Expression field mapping")

        def quote(value):
            return (
                '"'
                + value.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*").replace("_", "\\_")
                + '"'
            )

        knowledge = config.get(
            "known_words",
            [
                {
                    "query": f'deck:{quote(target["anki_deck_name"])} note:{quote(target["anki_note_type"])}',
                    "field": field,
                }
            ],
        )
        known = known_words(config, knowledge)
        shortlist = discover(sources, known, min(request["max_cards"] * 3, 1000))
    run = {
        "schema": 1,
        "run_id": uuid.uuid4().hex[:16],
        "config_hash": digest(config),
        "max_cards": request["max_cards"],
        "target": target,
        "sources": sources,
        "candidates": shortlist,
    }
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "run.json", run)
    # This is the only file the agent needs to read for semantic selection.
    write_json(folder / "shortlist.json", {"max_cards": run["max_cards"], "candidates": shortlist})
    return {
        "ok": True,
        "run": str(folder),
        "shortlist": str(folder / "shortlist.json"),
        "candidates": len(shortlist),
        "known_words": len(known),
        "max_cards": run["max_cards"],
        "deck": target["anki_deck_name"],
    }


@contextmanager
def run_lock(folder):
    """OS lock releases on process exit; a durable running receipt prevents replay."""
    with (folder / ".lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("This run is already being committed") from exc
        yield


def build_job(config, run, selected, folder):
    groups = {}
    for candidate in selected:
        groups.setdefault(candidate["source"], []).append(candidate)
    episodes = []
    for index, candidates in groups.items():
        source = run["sources"][index]
        episodes.append(
            {
                "run_id": f'{run["run_id"]}-{index}',
                **{
                    key: source[key]
                    for key in ("video_file", "subtitle_file", "subtitle_offset", "audio_track_override")
                },
                "tags": f'anki_miner_lean::{run["run_id"]}',
                "words": [
                    {"word": candidate["word"], "line_start": candidate["line_start"]}
                    for candidate in candidates
                ],
            }
        )
    return {
        "schema": 1,
        "language": "ja",
        "profile": config["profile"],
        "config": run["target"],
        "run_dir": str(folder / "upstream"),
        "episodes": episodes,
    }


def collect_receipt(folder, run, selected, job, verdict=None, error=None):
    outputs = []
    run_errors = []
    media_failures = 0
    global_refusal = (
        isinstance(verdict, dict)
        and verdict.get("ok") is False
        and verdict.get("error")
        in {"BUSY", "BAD_ARGUMENTS", "BAD_RUN_FILE", "PROFILE_UNREADABLE", "SETUP_ERROR", "ANKI_UNREACHABLE"}
        and verdict.get("runs") == []
    )
    expected_statuses = {
        "created",
        "duplicate",
        "refused",
        "no_definition",
        "media_failed",
        "not_found",
        "not_attempted",
        "uncertain",
    }
    for episode in job["episodes"]:
        index = int(episode["run_id"].rsplit("-", 1)[1])
        candidates = [candidate for candidate in selected if candidate["source"] == index]
        result_path = folder / "upstream" / episode["run_id"] / "result-1.json"
        results = None
        if result_path.is_file():
            result = read_json(result_path)
            require(
                result.get("schema") == 1 and result.get("run_id") == episode["run_id"],
                f"Invalid upstream result identity: {result_path}",
            )
            require(result.get("outcome") in {"success", "failed", "cancelled"}, "Invalid upstream outcome")
            if result["outcome"] != "success":
                run_errors.append(
                    {
                        "run_id": episode["run_id"],
                        "error": result.get("error"),
                        "message": result.get("message"),
                        "outcome": result["outcome"],
                    }
                )
            media_failures += result.get("media_store_failures", 0)
            results = result.get("words")
            require(
                isinstance(results, list) and len(results) == len(candidates),
                f"Upstream results do not align with the selection: {result_path}",
            )
        for position, candidate in enumerate(candidates):
            row = (
                results[position]
                if results is not None
                else {
                    "word": candidate["word"],
                    "status": "not_attempted" if global_refusal else "uncertain",
                    "note_id": None,
                }
            )
            require(
                isinstance(row, dict)
                and row.get("word") == candidate["word"]
                and row.get("status") in expected_statuses,
                "Invalid per-word upstream result",
            )
            if row["status"] == "created":
                require(type(row.get("note_id")) is int and row["note_id"] > 0, "Missing created note ID")
            outputs.append(
                {
                    "candidate_id": candidate["id"],
                    "word": candidate["word"],
                    "status": row["status"],
                    "note_id": row.get("note_id"),
                    "media_missing": row.get("media_missing", []),
                }
            )
    counts = {"selected": len(selected), "created": 0, "duplicate": 0, "failed": 0, "uncertain": 0}
    for row in outputs:
        key = row["status"] if row["status"] in {"created", "duplicate", "uncertain"} else "failed"
        counts[key] += 1
    status = "uncertain" if counts["uncertain"] else "failed" if counts["failed"] else "completed"
    if status == "failed" and counts["created"] + counts["duplicate"]:
        status = "partial"
    if status == "completed" and (run_errors or media_failures or verdict is not None and not verdict["ok"]):
        status = "partial"
    return {
        "ok": status == "completed",
        "status": status,
        "run_hash": digest(run),
        "selection": sorted(candidate["id"] for candidate in selected),
        "counts": counts,
        "outputs": outputs,
        "deck": run["target"]["anki_deck_name"],
        "browser_query": f'tag:anki_miner_lean::{run["run_id"]}',
        "error": (error if status == "uncertain" else None) or (verdict or {}).get("message"),
        "run_errors": run_errors,
        "media_store_failures": media_failures,
        "upstream_verdict": verdict,
    }


def commit(config, folder, selection):
    folder = Path(folder).expanduser().resolve(strict=True)
    object_keys(selection, {"candidate_ids"}, {"candidate_ids"})
    ids = selection["candidate_ids"]
    require(
        isinstance(ids, list) and all(isinstance(value, str) for value in ids),
        "candidate_ids must be an array of IDs from shortlist.json",
    )
    require(len(ids) == len(set(ids)), "Selection contains duplicate IDs")
    with run_lock(folder):
        run = read_json(folder / "run.json")
        require(run.get("schema") == 1, "Unsupported prepared-run schema")
        require(len(ids) <= run["max_cards"], "Selection exceeds the authorized max_cards")
        candidates = {candidate["id"]: candidate for candidate in run["candidates"]}
        require(set(ids) <= set(candidates), "Selection contains an ID outside this run")
        selected = [candidates[key] for key in sorted(ids)]
        receipt_path = folder / "receipt.json"
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            require(
                receipt["selection"] == sorted(ids) and receipt["run_hash"] == digest(run),
                "A committed run cannot change; prepare a new run",
            )
            if receipt["status"] in {"running", "uncertain"}:
                # Recover finished upstream files after an interruption. Never dispatch twice.
                receipt = collect_receipt(
                    folder,
                    run,
                    selected,
                    read_json(folder / "request.json"),
                    receipt.get("upstream_verdict"),
                    receipt.get("error"),
                )
                write_json(receipt_path, receipt)
            return receipt
        require(run["config_hash"] == digest(config), "Configuration changed; prepare a new run")
        checked = set()
        for candidate in selected:
            source = run["sources"][candidate["source"]]
            for key in ("video_file", "subtitle_file"):
                if source[key] not in checked:
                    require(
                        fingerprint(source[key]) == source[key + "_fingerprint"],
                        f"Source changed; prepare a new run: {source[key]}",
                    )
                    checked.add(source[key])
        job = build_job(config, run, selected, folder)
        (folder / "upstream").mkdir(exist_ok=True)
        write_json(folder / "request.json", job)
        write_json(receipt_path, {"status": "running", "run_hash": digest(run), "selection": sorted(ids)})
        verdict, error = None, None
        try:
            if selected:
                verdict = upstream(
                    config, "mine", folder / "request.json", log=folder / "upstream.log", timeout=12 * 60 * 60
                )
            receipt = collect_receipt(folder, run, selected, job, verdict)
        except Exception as exc:
            # The write may already have happened. Keep the reservation and stop.
            error = str(exc)
            receipt = {
                "ok": False,
                "status": "uncertain",
                "run_hash": digest(run),
                "selection": sorted(ids),
                "error": error,
                "upstream_verdict": verdict,
            }
        write_json(receipt_path, receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the separately installed upstream application")
    prep = commands.add_parser("prepare", help="Write a bounded subtitle shortlist to a new run folder")
    prep.add_argument("--request", type=Path, required=True)
    prep.add_argument("--out", type=Path, required=True)
    mine = commands.add_parser("commit", help="Mine selected IDs once; unchanged retries only read results")
    mine.add_argument("--run", type=Path, required=True)
    mine.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = doctor(config)
        elif args.command == "prepare":
            result = prepare(config, read_json(args.request), args.out)
        else:
            receipt = commit(config, args.run, read_json(args.selection))
            result = {
                key: receipt[key]
                for key in ("ok", "status", "counts", "deck", "browser_query", "error")
                if key in receipt
            }
            result["receipt"] = str(args.run.resolve() / "receipt.json")
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"ok": False, "error": "Interrupted; retry the same selection to inspect saved results"}
            )
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
