"""Window-free command facade for standalone media operations.

The functions in this module add only CLI-boundary validation and structured
result mapping. Media policy and processing remain in the existing services.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from anki_miner.config import AnkiMinerConfig
from anki_miner.headless.errors import HeadlessCommandError

_AUDIO_FORMATS = frozenset({"mp3", "opus", "flac"})


def load_active_config() -> AnkiMinerConfig:
    """Load the same active profile-backed configuration as the application."""
    from anki_miner.gui.utils.config_manager import GUIConfigManager

    return GUIConfigManager.load_config()


def _input_file(value: Path, label: str) -> Path:
    path = value.expanduser().resolve()
    if not path.is_file():
        raise HeadlessCommandError(
            "invalid_input",
            f"{label} is not a readable file: {path}",
            exit_code=2,
            details={"path": str(path)},
        )
    return path


def _output_file(
    value: Path,
    label: str,
    *,
    overwrite: bool,
    distinct_from: tuple[Path, ...] = (),
) -> Path:
    path = value.expanduser().resolve()
    if path in distinct_from:
        raise HeadlessCommandError(
            "unsafe_output",
            f"{label} must be distinct from every input file",
            exit_code=2,
            details={"path": str(path)},
        )
    if path.exists() and not path.is_file():
        raise HeadlessCommandError(
            "invalid_output",
            f"{label} is not a file path: {path}",
            exit_code=2,
            details={"path": str(path)},
        )
    if path.exists() and not overwrite:
        raise HeadlessCommandError(
            "output_exists",
            f"{label} already exists; pass --overwrite to replace it",
            exit_code=2,
            details={"path": str(path)},
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HeadlessCommandError(
            "invalid_output",
            f"Cannot create the output directory: {exc}",
            exit_code=2,
            details={"path": str(path.parent)},
        ) from exc
    return path


def probe_media(config: AnkiMinerConfig, media: Path) -> dict[str, Any]:
    """Return duration and bounded audio/subtitle track metadata."""
    from anki_miner.utils.audio_track_detector import (
        get_media_duration_seconds,
        list_audio_streams,
        list_subtitle_streams,
    )
    from anki_miner.utils.ffmpeg_resolver import resolve_ffprobe

    path = _input_file(media, "MEDIA")
    ffprobe = resolve_ffprobe(config)
    duration = get_media_duration_seconds(path, ffprobe)
    audio = list_audio_streams(path, ffprobe)
    subtitles = list_subtitle_streams(path, ffprobe)
    if duration is None:
        raise HeadlessCommandError(
            "probe_failed",
            "ffprobe could not determine the media duration",
            details={"path": str(path)},
        )
    return {
        "path": str(path),
        "duration_seconds": duration,
        "size_bytes": path.stat().st_size,
        "audio_tracks": [asdict(stream) for stream in audio],
        "subtitle_tracks": [asdict(stream) for stream in subtitles],
    }


def retime_media_subtitle(
    config: AnkiMinerConfig,
    video: Path,
    subtitle: Path,
    output: Path,
    *,
    overwrite: bool,
    reference_kind: Literal["subtitle", "audio"] | None,
    reference_index: int | None,
    cancel_event: threading.Event,
    log_cb,
) -> dict[str, Any]:
    """Validate paths, then delegate one retime operation to the service."""
    from anki_miner.services.retime_reference import ReferenceOverride
    from anki_miner.services.subtitle_retimer import retime_subtitle

    video_path = _input_file(video, "VIDEO")
    subtitle_path = _input_file(subtitle, "INPUT_SUBTITLE")
    output_path = _output_file(
        output,
        "OUTPUT_SUBTITLE",
        overwrite=overwrite,
        distinct_from=(video_path, subtitle_path),
    )
    if output_path.suffix.lower() != subtitle_path.suffix.lower():
        raise HeadlessCommandError(
            "invalid_output",
            "OUTPUT_SUBTITLE must use the same subtitle extension as INPUT_SUBTITLE",
            exit_code=2,
            details={"expected_suffix": subtitle_path.suffix.lower()},
        )
    override = (
        ReferenceOverride(reference_kind, reference_index)
        if reference_kind is not None and reference_index is not None
        else None
    )
    outcome = retime_subtitle(
        config,
        video_path,
        subtitle_path,
        output_path,
        reference_override=override,
        cancel_event=cancel_event,
        log_cb=log_cb,
    )
    if outcome.cancelled:
        raise HeadlessCommandError("cancelled", "Retiming was cancelled", exit_code=6)
    if not outcome.ok:
        raise HeadlessCommandError(
            "retime_failed",
            outcome.reason or "No trustworthy subtitle alignment was produced",
            details={
                "reference": outcome.reference_label,
                "attempts": list(outcome.attempts),
                "input_unchanged": True,
            },
        )
    return {
        "output": str(output_path),
        "engine": outcome.engine,
        "reference": outcome.reference_label,
        "attempts": list(outcome.attempts),
    }


def condense_media(
    config: AnkiMinerConfig,
    media: Path,
    subtitle: Path | None,
    output: Path,
    *,
    overwrite: bool,
    padding_ms: int,
    offset_ms: int,
    bitrate_kbps: int,
    filtered_chars: str,
    write_subtitles: bool,
    audio_track: int | None,
    subtitle_track: int | None,
    cancel_event: threading.Event,
    progress_cb,
) -> dict[str, Any]:
    """Validate paths, then delegate one condensed-audio operation."""
    from anki_miner.services.audio_condenser import (
        AudioCondenserService,
        CondenseStatus,
        condense_one,
    )

    media_path = _input_file(media, "MEDIA")
    subtitle_path = _input_file(subtitle, "SUBTITLE") if subtitle is not None else None
    distinct = (media_path,) if subtitle_path is None else (media_path, subtitle_path)
    output_path = _output_file(output, "OUTPUT_AUDIO", overwrite=overwrite, distinct_from=distinct)
    output_format = output_path.suffix.lower().lstrip(".")
    if output_format not in _AUDIO_FORMATS:
        raise HeadlessCommandError(
            "invalid_output",
            "OUTPUT_AUDIO must end in .mp3, .opus, or .flac",
            exit_code=2,
        )
    result = condense_one(
        AudioCondenserService(config),
        config,
        media_path,
        subtitle_path,
        output_path,
        offset_ms=offset_ms,
        padding_ms=padding_ms,
        filtered_chars=filtered_chars,
        bitrate_kbps=bitrate_kbps,
        audio_track_override=audio_track,
        subtitle_track_override=subtitle_track,
        write_subs=write_subtitles,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
    if result.status is CondenseStatus.CANCELLED:
        raise HeadlessCommandError("cancelled", "Condensing was cancelled", exit_code=6)
    if result.status is not CondenseStatus.SUCCESS or result.out_audio is None:
        details = {"status": result.status.name.lower()}
        if result.codecs:
            details["codecs"] = result.codecs
        if result.failure_reason:
            details["reason"] = result.failure_reason
        raise HeadlessCommandError(
            "condense_failed",
            result.failure_reason or f"Condensing ended with {result.status.name.lower()}",
            details=details,
        )
    return {
        "output": str(result.out_audio),
        "subtitle_outputs": (
            [str(output_path.with_suffix(".srt")), str(output_path.with_suffix(".lrc"))]
            if write_subtitles and result.sidecar_error is None
            else []
        ),
        "warnings": [warning for warning in (result.sidecar_error, result.tag_error) if warning is not None],
    }


def transcribe_media(
    config: AnkiMinerConfig,
    media: Path,
    output: Path,
    *,
    overwrite: bool,
    audio_track: int | None,
    cancel_event: threading.Event,
    progress_cb,
) -> dict[str, Any]:
    """Generate one subtitle file with the configured ASR backend."""
    from anki_miner.languages.registry import config_language, get_profile
    from anki_miner.services.asr.subtitle_generation import (
        SubtitleGenStatus,
        generate_subtitle_one,
    )
    from anki_miner.services.media_extractor import MediaExtractorService

    media_path = _input_file(media, "MEDIA")
    output_path = _output_file(output, "OUTPUT_SUBTITLE", overwrite=overwrite, distinct_from=(media_path,))
    if output_path.suffix.lower() != ".srt":
        raise HeadlessCommandError(
            "invalid_output",
            "OUTPUT_SUBTITLE must end in .srt",
            exit_code=2,
        )
    language = get_profile(config_language(config)).asr_language
    result = generate_subtitle_one(
        config,
        MediaExtractorService(config),
        media_path,
        output_path,
        transcribe_progress_cb=progress_cb,
        cancel_event=cancel_event,
        audio_track_override=audio_track,
        language=language,
    )
    if result.status is SubtitleGenStatus.CANCELLED:
        raise HeadlessCommandError("cancelled", "Transcription was cancelled", exit_code=6)
    if result.status is not SubtitleGenStatus.SUCCESS or result.out_srt is None:
        raise HeadlessCommandError(
            "transcribe_failed",
            f"Transcription ended with {result.status.name.lower()}",
            details={"status": result.status.name.lower()},
        )
    return {"output": str(result.out_srt), "language": language}


def download_media(
    config: AnkiMinerConfig,
    url: str,
    output_dir: Path,
    *,
    preset: str | None,
    format_selector: str | None,
    write_subtitles: bool | None,
    subtitle_languages: str | None,
    embed_thumbnail: bool | None,
    embed_metadata: bool | None,
    cancel_event: threading.Event,
    progress_cb,
) -> dict[str, Any]:
    """Download one URL using the configured generic downloader service."""
    from anki_miner.services.media_downloader import (
        FORMAT_PRESETS,
        DownloadOptions,
        DownloadStatus,
        MediaDownloaderService,
    )

    if len(url) > 8192 or urlsplit(url).scheme.lower() not in {"http", "https"} or not urlsplit(url).netloc:
        raise HeadlessCommandError("invalid_url", "URL must be an HTTP or HTTPS URL", exit_code=2)
    destination = output_dir.expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise HeadlessCommandError("invalid_output", "OUTPUT_DIR is not a directory", exit_code=2)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HeadlessCommandError(
            "invalid_output",
            f"Cannot create OUTPUT_DIR: {exc}",
            exit_code=2,
        ) from exc

    preset_key = preset or config.downloader_format_preset
    if preset_key not in FORMAT_PRESETS:
        raise HeadlessCommandError(
            "invalid_preset",
            f"Unknown download preset: {preset_key}",
            exit_code=2,
        )
    selected_format, preset_audio_format = FORMAT_PRESETS[preset_key]
    configured_custom = config.downloader_custom_format.strip()
    # Explicit CLI choices win over the saved GUI custom selector. Without
    # this distinction, ``--preset 720p`` could silently run an unrelated
    # custom format left in the active profile.
    custom_selector = format_selector or (configured_custom if preset is None else "")
    effective_selector = custom_selector or selected_format
    options = DownloadOptions(
        format_selector=effective_selector,
        extract_audio_format=None if custom_selector else preset_audio_format,
        write_subtitles=config.downloader_write_subtitles if write_subtitles is None else write_subtitles,
        subtitle_langs=subtitle_languages or config.downloader_subtitle_langs,
        embed_thumbnail=config.downloader_embed_thumbnail if embed_thumbnail is None else embed_thumbnail,
        embed_metadata=config.downloader_embed_metadata if embed_metadata is None else embed_metadata,
    )
    result = MediaDownloaderService(config).download(
        url,
        destination,
        options,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
    if result.status is DownloadStatus.CANCELLED:
        raise HeadlessCommandError("cancelled", "Download was cancelled", exit_code=6)
    if result.filepath is None:
        raise HeadlessCommandError(
            "download_output_unknown",
            "yt-dlp completed without reporting the output path",
        )
    path = result.filepath.expanduser()
    if not path.is_absolute():
        path = destination / path
    return {"status": result.status.value, "output": str(path.resolve())}
