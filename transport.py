"""File records and calls into the separate upstream Python environment."""

import json
import os
import subprocess
from pathlib import Path

MAX_JSON_BYTES = 8 * 1024 * 1024


def read_json(path):
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} bytes: {path}")
    return json.loads(data.decode("utf-8-sig"))


def write_json(path, value):
    """Replace one local record atomically, including after an interrupted write."""
    import tempfile

    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} bytes: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def invoke(config, arguments, *, log, timeout=60):
    """Upstream exits zero for API refusals too; inspect the JSON verdict."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    try:
        with Path(log).open("ab") as errors:
            result = subprocess.run(
                [config["upstream_python"], "-I", *map(str, arguments)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=environment,
                timeout=timeout,
                check=False,
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Upstream executable not found: {config['upstream_python']}. "
            "Install upstream Anki Miner separately and set upstream_python in config.json."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Upstream timed out; inspect saved results before attempting any new run") from exc
    if result.returncode:
        details = Path(log).read_text(encoding="utf-8", errors="replace")[-800:].strip()
        raise RuntimeError(
            f"Upstream exited {result.returncode}. Install upstream in upstream_python. {details}"
        )
    if len(result.stdout) > MAX_JSON_BYTES:
        raise RuntimeError("Upstream returned an oversized verdict")
    verdict = json.loads(result.stdout.decode("utf-8-sig"))
    if (
        not isinstance(verdict, dict)
        or type(verdict.get("schema")) is not int
        or verdict["schema"] != 1
        or type(verdict.get("ok")) is not bool
    ):
        raise RuntimeError("Unsupported upstream API verdict; schema 1 is required")
    return verdict


def require_success(verdict):
    if not verdict["ok"]:
        raise RuntimeError(f"Upstream {verdict.get('error')}: {verdict.get('message')}")
    return verdict.get("result", {})


def upstream(config, *arguments, log, timeout=60):
    return invoke(config, ["-m", "anki_miner", "--api", *arguments], log=log, timeout=timeout)


def bridge(config, command, *, log, **request):
    import tempfile

    with tempfile.TemporaryDirectory(prefix="anki-miner-bridge-") as temporary:
        path = Path(temporary) / "request.json"
        write_json(
            path,
            {"command": command, "profile": config["profile"], "language": config["language"], **request},
        )
        return require_success(
            invoke(config, [Path(__file__).with_name("candidates.py"), path], log=log, timeout=3600)
        )
