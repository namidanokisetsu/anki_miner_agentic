from __future__ import annotations

import pytest

from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AgentProfileConfig, KnowledgeSource, LocalEpisodeInput, WriteTarget, YouTubeInput


def test_japanese_audio_is_the_profile_default(tmp_path):
    config = AgentProfileConfig(
        (KnowledgeSource("Deck", "ExampleNote", ("word",), ("sentence",)),),
        WriteTarget("Destination", "ExampleNote"),
    )
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.touch()
    subtitle.touch()

    episode = LocalEpisodeInput(video, subtitle)

    assert config.audio_track == "japanese"
    assert episode.audio_track is None


def test_audio_track_override_is_zero_based_and_validated(tmp_path):
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.touch()
    subtitle.touch()
    assert LocalEpisodeInput(video, subtitle, audio_track=1).audio_track == 1
    with pytest.raises(AgentMiningError):
        LocalEpisodeInput(video, subtitle, audio_track=-1)


def test_local_subtitle_offset_is_numeric_and_source_specific(tmp_path):
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.touch()
    subtitle.touch()

    assert (
        LocalEpisodeInput.from_dict(
            {"video_file": str(video), "subtitle_file": str(subtitle), "subtitle_offset": -2.5}
        ).subtitle_offset
        == -2.5
    )
    with pytest.raises(AgentMiningError):
        LocalEpisodeInput(video, subtitle, subtitle_offset=True)
    with pytest.raises(AgentMiningError):
        LocalEpisodeInput(video, subtitle, subtitle_offset=301)


def test_youtube_permission_flags_require_json_booleans():
    with pytest.raises(AgentMiningError, match="allow_asr must be boolean"):
        YouTubeInput.from_dict({"url": "https://www.youtube.com/watch?v=fixture", "allow_asr": "false"})
