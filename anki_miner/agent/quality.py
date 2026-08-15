"""Qt-independent transcript quality checks used by headless preparation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from anki_miner.utils.ja_normalize import is_cjk_ideograph

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_REPEATED_CHUNK_RE = re.compile(r"(.{2,8})\1{2,}")


@dataclass(frozen=True)
class CueQuality:
    flags: tuple[str, ...]
    severe_error: str | None = None


def assess_subtitle_cue(text: str, start_s: float, end_s: float, source: str) -> CueQuality:
    if start_s < 0 or end_s <= start_s:
        return CueQuality((), "malformed_timing")
    flags: list[str] = []
    if source in {"youtube_auto", "local_asr"}:
        flags.append("automatic_transcript")
    duration = end_s - start_s
    compact = "".join(text.split())
    if compact and len(compact) / duration > 18:
        flags.append("implausible_reading_speed")
    language_chars = [char for char in compact if char.isalpha() or is_cjk_ideograph(char)]
    japanese = sum(1 for char in language_chars if _KANA_RE.match(char) or is_cjk_ideograph(char))
    if language_chars and japanese / len(language_chars) < 0.35:
        flags.append("low_japanese_ratio")
    if _REPEATED_CHUNK_RE.search(compact):
        flags.append("repetitive_transcript")
    if duration > 20:
        flags.append("long_cue")
    return CueQuality(tuple(flags))
