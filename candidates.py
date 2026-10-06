"""Candidate-export bridge, executed inside the upstream Python environment.

Only this file imports upstream internals. Parsing, filtering and cue merging
remain upstream-owned; returning [] at curation stops before media/card writes.
"""

import hashlib
import json
import logging
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path


def load_settings(request):
    from anki_miner import __version__
    from anki_miner.cli.api.settings import load_profile_config, with_language
    from anki_miner.gui.utils.config_manager import GUIConfigManager

    config = with_language(load_profile_config(request["profile"]), request["language"], code="BAD_ARGUMENTS")
    state = {
        "version": __version__,
        "settings_hash": hashlib.sha256(
            json.dumps(
                GUIConfigManager._config_to_serializable_dict(config), sort_keys=True, default=str
            ).encode()
        ).hexdigest(),
    }
    return replace(config, allow_duplicate_cards=False), state


def export_rows(processor, source, source_index, words):
    from anki_miner.cli.api.lines import cue_index
    from anki_miner.services.word_filter import merge_cue_window

    parser = processor.subtitle_parser
    path, offset = Path(source["subtitle_file"]), source["subtitle_offset"]
    entries = parser.parse_raw_entries(path, offset)
    raw = parser.parse_raw_entries(path, 0.0)
    if len(raw) != len(entries):
        raise RuntimeError("Upstream cue timelines no longer align")
    rows = []
    for word in words:
        index = cue_index(entries, word)
        if index is None:
            raise RuntimeError(f"Cannot preserve upstream's chosen cue for {word.mined_form!r}")
        window = merge_cue_window(entries, index, *word.line_expansion)
        rows.append(
            {
                "word": word.mined_form,
                "sentence": window.text,
                "source": source_index,
                # API times are unshifted: subtraction fails when upstream clamps at zero.
                "line_start": raw[index][0],
                # Explicit [0, 0] matters: i+1 may have refused an automatic merge.
                "line_expansion": list(word.line_expansion),
                "occurrences": word.occurrence_count,
                "frequency_rank": word.frequency_rank,
                "unknown_words_in_cue": word.line_unknown_count,
            }
        )
    return rows


def forbid_writes(*args, **kwargs):
    raise RuntimeError("Candidate export must stop before media extraction or card creation")


def discover(config, sources, limit):
    from anki_miner.gui.utils.service_factory import create_episode_processor
    from anki_miner.models import MiningOutcome, classify_result
    from anki_miner.presenters.null_presenter import NullPresenter

    processor = create_episode_processor(config, NullPresenter(), stats_service=None)
    # Backstops if a future upstream release changes the callback's early exit.
    processor._phase3_extract = forbid_writes
    processor._phase5_create = forbid_writes
    rows, seen = [], set()
    total = 0
    try:
        for source_index, source in enumerate(sources):

            def capture(words):
                nonlocal total
                for row in export_rows(processor, source, source_index, words):
                    if row["word"] in seen:
                        continue
                    seen.add(row["word"])
                    total += 1
                    if len(rows) < limit:
                        rows.append({"id": f"c{len(rows) + 1:04d}", **row})
                return []

            capture.suppress_curation_messages = True
            result = processor.process_episode(
                Path(source["video_file"]),
                Path(source["subtitle_file"]),
                subtitle_offset=source["subtitle_offset"],
                curation_callback=capture,
            )
            if classify_result(result) != MiningOutcome.SUCCESS or result.cards_created:
                raise RuntimeError(f"Upstream candidate discovery failed: {result.errors}")
    finally:
        processor.close()
    return {"candidates": rows, "total_candidates": total}


def execute(request):
    from anki_miner.cli.entry import _prepare_process, acquire_run_lock

    _prepare_process()
    # Import adapter dependencies even for inspect/doctor.
    from anki_miner.cli.api.lines import cue_index  # noqa: F401
    from anki_miner.gui.utils.config_manager import GUIConfigManager
    from anki_miner.gui.utils.service_factory import create_episode_processor  # noqa: F401

    if request["command"] == "inspect":
        _, state = load_settings(request)
        return {"upstream_state": state}
    if request["command"] != "discover":
        raise ValueError("Unknown bridge command")
    lock = acquire_run_lock()
    try:
        config, state = load_settings(request)
        serialized = GUIConfigManager._config_to_serializable_dict(config)
        target = {
            key: serialized[key]
            for key in (
                "anki_deck_name",
                "anki_note_type",
                "anki_fields",
                "card_type",
                "card_type_marker_fields",
            )
        }
        target["allow_duplicate_cards"] = False
        return {
            "upstream_state": state,
            "target": target,
            **discover(config, request["sources"], request["limit"]),
        }
    finally:
        lock.unlock()


def main():
    logging.basicConfig(level=logging.WARNING)
    try:
        path = Path(sys.argv[1])
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Bridge request exceeds 8 MiB")
        request = json.loads(path.read_text(encoding="utf-8-sig"))
        with redirect_stdout(sys.stderr):
            result = execute(request)
        verdict = {"schema": 1, "ok": True, "result": result}
    except Exception as exc:
        logging.exception("Upstream candidate bridge failed")
        verdict = {"schema": 1, "ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(verdict, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
