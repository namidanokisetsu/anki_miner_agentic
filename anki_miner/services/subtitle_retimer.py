"""Subtitle retiming orchestrator: clean → align → validate → commit.

One episode's retime is a pipeline, not a single tool call:

1. **Reference** — :mod:`anki_miner.services.retime_reference` picks what to
   align against (embedded dialogue track preferred, extracted audio fallback,
   raw video as last resort).
2. **Clean** — :mod:`anki_miner.services.subtitle_cleaner` strips non-dialogue
   cues (signs, songs, ♪ markers, HoH annotations) from the input into a copy
   the aligners can read; aligners see dialogue only. A format no engine reads
   (WebVTT: alass takes SubRip/SSA/VobSub only) is transcoded to SRT here, and
   step 4 puts the result back onto the original in its own format.
3. **Align** — engines are tried in order until one produces a candidate that
   survives validation: ffsubsync in split mode (in-process, has its own
   quality gate), then alass in split mode, then alass with a single global
   offset, then ffsubsync with a single global offset. A missing alass binary
   (macOS) skips straight from ffsubsync split mode to ffsubsync single-offset,
   so alignment still gets two attempts, not one.
4. **Map back** — the winning candidate's timings are applied to the untouched
   original, so every line and all ASS styling survive.
5. **Validate** — :mod:`anki_miner.services.sync_validator` rejects the failure
   signatures aligners produce when they lock onto a wrong optimum. A rejected
   candidate is discarded; the chain moves on.
6. **Commit** — only a validated candidate replaces *out_sub*, atomically.
   *out_sub* must be a path of its own (callers derive it from the input's name
   with a ``_retimed`` suffix), so committing never touches the input.

The guarantee callers build UX on: **a bad sync never overwrites a usable
subtitle** — when every engine fails validation the original files are left
exactly as they were and the outcome says why.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from anki_miner.exceptions.subtitle import AlassNotFoundError
from anki_miner.services.retime_reference import ReferenceOverride, resolve_reference
from anki_miner.services.subtitle_cleaner import (
    clean_for_alignment,
    map_deltas_back,
    transcode_for_alignment,
)
from anki_miner.services.sync_engines import SyncResult
from anki_miner.services.sync_engines.alass_engine import sync_with_alass
from anki_miner.services.sync_engines.ffsubsync_engine import sync_with_ffsubsync
from anki_miner.services.sync_validator import validate_candidate
from anki_miner.utils.audio_track_detector import get_media_duration_seconds
from anki_miner.utils.ffmpeg_resolver import resolve_ffprobe
from anki_miner.utils.i18n import tr_format

logger = logging.getLogger(__name__)

__all__ = ["RetimeOutcome", "retime_subtitle"]

#: Root below ``out_sub.parent`` where each retime gets an isolated workspace.
#: Keeping real subtitle temps below this hidden directory makes them invisible
#: to ``FilePairMatcher`` while keeping them on the output filesystem, so
#: ``_commit`` cannot hit EXDEV. The root itself is intentionally retained:
#: deleting a shared root lets one concurrent run remove another run's empty
#: workspace between creation and its first write.
TMP_SUBDIR_NAME = ".anki-miner-retime-tmp"

# Leave room below MAX_PATH for alass's own path handling and suffixes. Python
# itself supports longer Windows paths, but alass v2.0.0 still fails to open
# them unless its working files live under a short path.
_WINDOWS_WORK_PATH_LIMIT = 240

#: Formats every alignment engine can read and write. alass v2.0.0 accepts only
#: SubRip, SubStationAlpha and VobSub -- handed a .vtt it prints "unknown
#: subtitle format", exits 1 and writes nothing, costing the chain two of its
#: four engines. ffsubsync does read and write WebVTT, but the alignment temps
#: are normalized unconditionally so every engine sees one format.
_ALIGNER_FORMATS: frozenset[str] = frozenset({".srt", ".ass", ".ssa"})


def _alignment_suffix(in_sub: Path) -> str:
    """Suffix the alignment temps carry: the input's own, or .srt when no engine reads it.

    Internal to the pipeline. The committed output keeps the user's format --
    :func:`map_deltas_back` writes the untouched original, whose extension
    decides what pysubs2 emits.
    """
    suffix = in_sub.suffix.lower()
    return suffix if suffix in _ALIGNER_FORMATS else ".srt"


@dataclass(frozen=True)
class RetimeOutcome:
    """What one episode's retime pipeline did.

    Truthy exactly when a validated result was written. ``attempts`` holds one
    human-readable line per engine tried — the forensic trail the old pipeline
    never kept.
    """

    ok: bool
    engine: str | None = None
    reference_label: str | None = None
    attempts: tuple[str, ...] = ()
    reason: str = ""
    cancelled: bool = False

    def __bool__(self) -> bool:
        return self.ok


def retime_subtitle(
    config,
    video: Path,
    in_sub: Path,
    out_sub: Path,
    *,
    reference_override: ReferenceOverride | None = None,
    cancel_event: threading.Event | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> RetimeOutcome:
    """Retime *in_sub* to *video*, writing a validated result to *out_sub*.

    Engine order, validation, and the keep-original guarantee are described in
    the module docstring. Never raises for content or tool reasons; every
    failure path comes back as a falsy :class:`RetimeOutcome` with the original
    files untouched.

    Args:
        config: Application config (used to resolve tool paths and temp dirs).
        video:  The video the subtitle should be matched against.
        in_sub: The off-timed subtitle file to correct.
        out_sub: Destination path, distinct from *in_sub* — the caller owns that
            guarantee (the GUI worker derives the name with a ``_retimed``
            suffix). Passing *in_sub* here overwrites it with no copy kept.
        reference_override: Explicit user pick of the reference track; None
            auto-selects (embedded subtitle preferred, audio fallback).
        cancel_event: Checked between pipeline steps; alass runs are killed
            mid-flight, an in-process ffsubsync run finishes first.
        log_cb: Called with human-readable progress/decision lines.
    """
    logger.info("retime: %s <- %s (out %s)", video.name, in_sub.name, out_sub.name)
    local_tmp_root = out_sub.parent / TMP_SUBDIR_NAME
    tmp_root = _temp_root_for_output(out_sub)
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A volume root can be read-only even when the destination folder is
        # writable. Fall back to the local path; the aligner will report a
        # useful error if that path also exceeds a platform limit.
        tmp_root = local_tmp_root
        tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="run-", dir=tmp_root))
    temps: list[Path] = []
    reference = None
    try:
        tmp_dir.mkdir(exist_ok=True)
        duration_s = get_media_duration_seconds(video, resolve_ffprobe(config))
        reference = resolve_reference(
            config,
            video,
            override=reference_override,
            video_duration_seconds=duration_s,
            cancel_event=cancel_event,
            log_cb=log_cb,
        )
        if _cancelled(cancel_event):
            return RetimeOutcome(ok=False, cancelled=True, reason="cancelled")

        reference_label = reference.label if reference is not None else "raw video"
        reference_path = reference.path if reference is not None else video
        sub_reference = reference is not None and reference.kind == "subtitle"

        align_suffix = _alignment_suffix(in_sub)
        cleaned = clean_for_alignment(in_sub, tmp_dir / (out_sub.stem + ".retime-clean" + align_suffix))
        if cleaned is None and align_suffix != in_sub.suffix.lower():
            # Cleaning declined, but no engine can read this format as-is. Keep
            # every representable cue rather than dropping non-dialogue: the
            # cue floor is already unmet, so there is nothing to spare.
            cleaned = transcode_for_alignment(in_sub, tmp_dir / (out_sub.stem + ".retime-clean" + align_suffix))
        if cleaned is not None:
            temps.append(cleaned.path)
            if cleaned.dropped:
                _log(
                    log_cb,
                    tr_format(
                        QCoreApplication.translate(
                            "SubtitleRetimer", "Ignoring %1 non-dialogue lines during alignment."
                        ),
                        cleaned.dropped,
                    ),
                )
        align_input = cleaned.path if cleaned is not None else in_sub

        attempts: list[str] = []
        alass_missing = False
        for label, runner in _engine_chain(config, sub_reference=sub_reference, cancel_event=cancel_event):
            if _cancelled(cancel_event):
                return RetimeOutcome(
                    ok=False,
                    cancelled=True,
                    reference_label=reference_label,
                    attempts=tuple(attempts),
                    reason="cancelled",
                )
            if alass_missing and label.startswith("alass"):
                continue

            # From align_input, not out_sub: the engine infers its output format
            # from this suffix, and _commit is a bare os.replace with no
            # transcode -- a candidate in a format the engine was not given
            # would be committed under the wrong extension. Identical to
            # out_sub.suffix for every format the engines read natively.
            candidate = tmp_dir / f"{out_sub.stem}.retime-cand-{len(attempts)}{align_input.suffix}"
            temps.append(candidate)
            try:
                result = runner(reference_path, align_input, candidate, log_cb)
            except AlassNotFoundError:
                alass_missing = True
                attempts.append(f"{label}: binary not installed")
                _log(
                    log_cb,
                    QCoreApplication.translate("SubtitleRetimer", "alass is not installed; skipping alass attempts."),
                )
                continue

            final_candidate = candidate
            if result.ok and cleaned is not None:
                mapped = tmp_dir / f"{out_sub.stem}.retime-map-{len(attempts)}{out_sub.suffix}"
                temps.append(mapped)
                if map_deltas_back(in_sub, candidate, cleaned.kept_indices, mapped):
                    final_candidate = mapped
                else:
                    attempts.append(f"{label}: aligner changed the cue count; discarded")
                    continue

            verdict = validate_candidate(in_sub, final_candidate, result, video_duration_seconds=duration_s)
            if verdict.ok:
                attempts.append(f"{label}: accepted")
                _commit(final_candidate, out_sub)
                summary = _success_summary(result)
                logger.info(
                    "retime: %s accepted for %s (reference %s)%s",
                    label,
                    video.name,
                    reference_label,
                    summary,
                )
                _log(log_cb, _success_message(label, result))
                return RetimeOutcome(
                    ok=True,
                    engine=result.engine,
                    reference_label=reference_label,
                    attempts=tuple(attempts),
                )

            reason = "; ".join(verdict.reasons) or "rejected"
            attempts.append(f"{label}: {reason}")
            _log(
                log_cb,
                tr_format(QCoreApplication.translate("SubtitleRetimer", "%1 result rejected: %2"), label, reason),
            )

        reason = QCoreApplication.translate(
            "SubtitleRetimer", "no engine produced a trustworthy sync; original left untouched"
        )
        logger.warning(
            "retime: kept original for %s (reference %s). Attempts: %s",
            video.name,
            reference_label,
            " | ".join(attempts) or "none",
        )
        return RetimeOutcome(
            ok=False,
            reference_label=reference_label,
            attempts=tuple(attempts),
            reason=reason,
        )
    finally:
        for temp in temps:
            _unlink_quiet(temp)
        if reference is not None and reference.temp is not None:
            _unlink_quiet(reference.temp)
        # Remove only this run's workspace. TMP_SUBDIR_NAME is shared by
        # concurrent runs and must remain present so no run can delete the
        # directory after another has created it but before its first write.
        with contextlib.suppress(OSError):
            tmp_dir.rmdir()


def _temp_root_for_output(out_sub: Path) -> Path:
    """Choose a same-volume work root that remains usable by Windows alass."""
    local_root = out_sub.parent / TMP_SUBDIR_NAME
    probe = local_root / "run-xxxxxxxx" / f"{out_sub.stem}.retime-clean{out_sub.suffix}"
    if os.name == "nt" and len(str(probe)) >= _WINDOWS_WORK_PATH_LIMIT and out_sub.anchor:
        return Path(out_sub.anchor) / TMP_SUBDIR_NAME
    return local_root


def _engine_chain(config, *, sub_reference: bool, cancel_event: threading.Event | None):
    """Yield ``(label, runner)`` pairs in the order they should be tried."""

    def run_ffsubsync(reference: Path, in_sub: Path, out: Path, log_cb) -> SyncResult:
        return sync_with_ffsubsync(config, reference, in_sub, out, cancel_event=cancel_event, log_cb=log_cb)

    def run_ffsubsync_offset(reference: Path, in_sub: Path, out: Path, log_cb) -> SyncResult:
        return sync_with_ffsubsync(
            config, reference, in_sub, out, split_mode=False, cancel_event=cancel_event, log_cb=log_cb
        )

    def run_alass_split(reference: Path, in_sub: Path, out: Path, log_cb) -> SyncResult:
        return sync_with_alass(
            config,
            reference,
            in_sub,
            out,
            sub_reference=sub_reference,
            no_split=False,
            cancel_event=cancel_event,
            log_cb=log_cb,
        )

    def run_alass_offset(reference: Path, in_sub: Path, out: Path, log_cb) -> SyncResult:
        return sync_with_alass(
            config,
            reference,
            in_sub,
            out,
            sub_reference=sub_reference,
            no_split=True,
            cancel_event=cancel_event,
            log_cb=log_cb,
        )

    # Alass's subtitle-to-subtitle path is both more accurate and far faster
    # than either engine's audio analysis. Prefer it for an extracted dialogue
    # subtitle; retain ffsubsync as the portable first choice for audio.
    if sub_reference:
        yield "alass", run_alass_split
        yield "ffsubsync", run_ffsubsync
    else:
        yield "ffsubsync", run_ffsubsync
        yield "alass", run_alass_split
    yield "alass (single offset)", run_alass_offset
    yield "ffsubsync (single offset)", run_ffsubsync_offset


def _commit(candidate: Path, out_sub: Path) -> None:
    """Atomically place *candidate* at *out_sub*.

    No copy is kept aside: callers write to a name derived from the input
    (``<stem>_retimed<ext>``), so anything already at *out_sub* is a previous
    retime of the same pair — reproducible by running again. The user's own
    subtitle is never the file being replaced.
    """
    os.replace(candidate, out_sub)


def _success_summary(result: SyncResult) -> str:
    if result.offset_seconds is not None:
        return f" (offset {result.offset_seconds:+.2f}s)"
    if result.block_shifts_seconds:
        shifts = result.block_shifts_seconds
        if len(shifts) == 1:
            return f" (offset {shifts[0]:+.2f}s)"
        return f" ({len(shifts)} blocks, shifts {min(shifts):+.2f}s..{max(shifts):+.2f}s)"
    return ""


def _success_message(label: str, result: SyncResult) -> str:
    """User-facing counterpart of :func:`_success_summary` — same numbers, translated."""
    if result.offset_seconds is not None:
        return tr_format(
            QCoreApplication.translate("SubtitleRetimer", "Retimed with %1 (offset %2)."),
            label,
            f"{result.offset_seconds:+.2f}s",
        )
    if result.block_shifts_seconds:
        shifts = result.block_shifts_seconds
        if len(shifts) == 1:
            return tr_format(
                QCoreApplication.translate("SubtitleRetimer", "Retimed with %1 (offset %2)."),
                label,
                f"{shifts[0]:+.2f}s",
            )
        return tr_format(
            QCoreApplication.translate("SubtitleRetimer", "Retimed with %1 (%2 blocks, shifts %3..%4)."),
            label,
            len(shifts),
            f"{min(shifts):+.2f}s",
            f"{max(shifts):+.2f}s",
        )
    return tr_format(QCoreApplication.translate("SubtitleRetimer", "Retimed with %1."), label)


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _log(log_cb: Callable[[str], None] | None, message: str) -> None:
    logger.info("retime: %s", message)
    if log_cb is not None:
        log_cb(message)


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
