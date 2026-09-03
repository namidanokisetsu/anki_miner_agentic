import json
import threading
from pathlib import Path

from anki_miner.config import AnkiMinerConfig
from anki_miner.headless.cli import main
from anki_miner.headless.media_commands import download_media


def _result(capsys):
    return json.loads(capsys.readouterr().out)["result"]


def test_probe_uses_active_config_and_prints_json(monkeypatch, capsys):
    config = AnkiMinerConfig()
    monkeypatch.setattr("anki_miner.headless.media_commands.load_active_config", lambda: config)

    def probe(active, media):
        assert active is config
        assert media == Path("episode.mkv")
        return {"duration_seconds": 120.0, "audio_tracks": [], "subtitle_tracks": []}

    monkeypatch.setattr("anki_miner.headless.media_commands.probe_media", probe)

    assert main(["probe", "episode.mkv"]) == 0
    assert _result(capsys)["duration_seconds"] == 120.0


def test_retime_forwards_explicit_reference_track(monkeypatch, capsys):
    config = AnkiMinerConfig()
    monkeypatch.setattr("anki_miner.headless.media_commands.load_active_config", lambda: config)

    def retime(active, video, subtitle, output, **kwargs):
        assert active is config
        assert (video, subtitle, output) == (Path("v.mkv"), Path("in.srt"), Path("out.srt"))
        assert kwargs["reference_kind"] == "subtitle"
        assert kwargs["reference_index"] == 2
        return {"output": str(output), "engine": "fixture"}

    monkeypatch.setattr("anki_miner.headless.media_commands.retime_media_subtitle", retime)

    assert (
        main(
            [
                "retime",
                "--video",
                "v.mkv",
                "--subtitle",
                "in.srt",
                "--output",
                "out.srt",
                "--reference-subtitle-track",
                "2",
            ]
        )
        == 0
    )
    assert _result(capsys)["engine"] == "fixture"


def test_condense_uses_active_profile_defaults(monkeypatch, capsys):
    config = AnkiMinerConfig(
        condenser_padding_ms=750,
        condenser_offset_ms=-125,
        condenser_bitrate_kbps=128,
        condenser_filtered_chars="♪",
        condenser_write_subtitles=True,
    )
    monkeypatch.setattr("anki_miner.headless.media_commands.load_active_config", lambda: config)

    def condense(active, media, subtitle, output, **kwargs):
        assert active is config
        assert kwargs["padding_ms"] == 750
        assert kwargs["offset_ms"] == -125
        assert kwargs["bitrate_kbps"] == 128
        assert kwargs["filtered_chars"] == "♪"
        assert kwargs["write_subtitles"] is True
        return {"output": str(output), "warnings": []}

    monkeypatch.setattr("anki_miner.headless.media_commands.condense_media", condense)

    assert main(["condense", "--media", "v.mkv", "--subtitle", "in.srt", "--output", "out.mp3"]) == 0
    assert _result(capsys)["warnings"] == []


def test_transcribe_and_download_dispatch(monkeypatch, capsys, tmp_path):
    config = AnkiMinerConfig()
    monkeypatch.setattr("anki_miner.headless.media_commands.load_active_config", lambda: config)
    monkeypatch.setattr(
        "anki_miner.headless.media_commands.transcribe_media",
        lambda active, media, output, **kwargs: {"output": str(output), "language": "ja"},
    )
    assert main(["transcribe", "--media", "v.mkv", "--output", "v.srt"]) == 0
    assert _result(capsys)["language"] == "ja"

    captured = {}

    def download(active, url, output_dir, **kwargs):
        captured.update(kwargs)
        return {"status": "done", "output": str(output_dir / "video.mkv")}

    monkeypatch.setattr("anki_miner.headless.media_commands.download_media", download)
    assert (
        main(
            [
                "download",
                "--url",
                "https://example.com/watch",
                "--output-dir",
                str(tmp_path),
                "--preset",
                "720p",
                "--write-subtitles",
            ]
        )
        == 0
    )
    assert captured["preset"] == "720p"
    assert captured["write_subtitles"] is True
    assert _result(capsys)["status"] == "done"


class _MiningApp:
    def __init__(self):
        self.closed = False

    def prepare_mining_run(self, request):
        assert request == {"inputs": [], "max_cards": 1}
        return {"run_id": "run-1", "state": "prepared", "shortlist": []}

    def close(self):
        self.closed = True


def test_mine_prepare_writes_file_backed_result(monkeypatch, capsys, tmp_path):
    request = tmp_path / "prepare.json"
    request.write_text(json.dumps({"inputs": [], "max_cards": 1}), encoding="utf-8")
    output = tmp_path / "prepared.json"
    app = _MiningApp()
    monkeypatch.setattr("anki_miner.headless.cli.build_agent_application", lambda _config: app)

    assert (
        main(
            [
                "--config",
                str(tmp_path / "agent.json"),
                "mine",
                "prepare",
                "--request",
                str(request),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    assert json.loads(output.read_text(encoding="utf-8"))["result"]["run_id"] == "run-1"
    assert capsys.readouterr().out == ""
    assert app.closed is True


def test_existing_receipt_is_refused_before_building_app(monkeypatch, capsys, tmp_path):
    request = tmp_path / "prepare.json"
    request.write_text("{}", encoding="utf-8")
    output = tmp_path / "prepared.json"
    output.write_text("keep", encoding="utf-8")
    built = False

    def build(_config):
        nonlocal built
        built = True
        return _MiningApp()

    monkeypatch.setattr("anki_miner.headless.cli.build_agent_application", build)
    assert (
        main(
            [
                "--config",
                str(tmp_path / "agent.json"),
                "mine",
                "prepare",
                "--request",
                str(request),
                "--output",
                str(output),
            ]
        )
        == 2
    )

    assert json.loads(capsys.readouterr().out)["error"]["code"] == "output_exists"
    assert output.read_text(encoding="utf-8") == "keep"
    assert built is False


def test_invalid_arguments_use_json_error_envelope(capsys):
    assert main(["retime", "--video", "v.mkv"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"


def test_explicit_download_preset_overrides_saved_custom_format(monkeypatch, tmp_path):
    from anki_miner.services.media_downloader import DownloadResult, DownloadStatus

    config = AnkiMinerConfig(downloader_custom_format="saved-custom-selector")
    captured = {}

    class Service:
        def __init__(self, active):
            assert active is config

        def download(self, url, destination, options, **kwargs):
            captured["options"] = options
            return DownloadResult(DownloadStatus.DONE, Path("episode.mkv"))

    monkeypatch.setattr("anki_miner.services.media_downloader.MediaDownloaderService", Service)

    result = download_media(
        config,
        "https://example.com/watch",
        tmp_path,
        preset="720p",
        format_selector=None,
        write_subtitles=None,
        subtitle_languages=None,
        embed_thumbnail=None,
        embed_metadata=None,
        cancel_event=threading.Event(),
        progress_cb=lambda _message, _fraction: None,
    )

    assert captured["options"].format_selector == "bestvideo[height<=720]+bestaudio/best[height<=720]"
    assert result["output"] == str((tmp_path / "episode.mkv").resolve())
