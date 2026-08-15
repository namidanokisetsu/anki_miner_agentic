from __future__ import annotations

from pathlib import Path

import pytest

from anki_miner.config import AnkiMinerConfig
from anki_miner.services.media_extractor import MediaExtractorService
from anki_miner.utils.audio_track_detector import JAPANESE_LANGUAGE_CODES, list_audio_streams


def test_supplied_dual_audio_sample_defaults_to_japanese():
    sample_dir = Path(__file__).parents[2] / "sample"
    videos = list(sample_dir.glob("*.mp4"))
    if not videos:
        pytest.skip("user-supplied dual-audio sample is not present")
    video = videos[0]
    service = MediaExtractorService(AnkiMinerConfig())
    streams = list_audio_streams(video)
    japanese = next(stream for stream in streams if stream.language_tag in JAPANESE_LANGUAGE_CODES)
    english = next(stream for stream in streams if stream.language_tag == "eng")

    assert service._resolve_audio_track_global_index(video, None) == japanese.global_index
    assert service._resolve_audio_track_global_index(video, english.audio_index) == english.global_index
