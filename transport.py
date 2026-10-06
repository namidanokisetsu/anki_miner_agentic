"""The two external boundaries: upstream's CLI and AnkiConnect HTTP."""

import html
import json
import os
import re
import subprocess
import urllib.request
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


def upstream(config, *arguments, log, timeout=60):
    """Upstream exits zero for API refusals too; inspect the JSON verdict."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    try:
        with Path(log).open("ab") as errors:
            result = subprocess.run(
                [*config["upstream"], "--api", *map(str, arguments)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=environment,
                timeout=timeout,
                check=False,
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Upstream executable not found: {config['upstream'][0]}. "
            "Install upstream Anki Miner separately and set upstream in config.json."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Upstream timed out; inspect saved results before attempting any new run") from exc
    if result.returncode:
        raise RuntimeError(f"Upstream exited {result.returncode}; see {log}")
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


def anki(config, action, **params):
    payload = {"action": action, "version": 6, "params": params}
    if config.get("anki_key_env"):
        payload["key"] = os.environ[config["anki_key_env"]]
    request = urllib.request.Request(
        config["anki_connect"],
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise RuntimeError("AnkiConnect response is too large; narrow the knowledge query")
    reply = json.loads(data)
    if not isinstance(reply, dict) or set(reply) != {"result", "error"}:
        raise RuntimeError("Malformed AnkiConnect response")
    if reply["error"] is not None:
        raise RuntimeError(f"AnkiConnect {action}: {reply['error']}")
    return reply["result"]


def known_words(config, sources):
    """Read exact expression fields, without copying a learner database."""
    words = set()
    for source in sources:
        ids = anki(config, "findNotes", query=source["query"])
        if not isinstance(ids, list) or any(type(note_id) is not int for note_id in ids):
            raise RuntimeError("AnkiConnect returned invalid note IDs")
        for offset in range(0, len(ids), 250):
            rows = anki(config, "notesInfo", notes=ids[offset : offset + 250])
            if not isinstance(rows, list) or len(rows) != len(ids[offset : offset + 250]):
                raise RuntimeError("Incomplete AnkiConnect notesInfo response")
            for row in rows:
                try:
                    value = row["fields"][source["field"]]["value"]
                except (KeyError, TypeError) as exc:
                    raise ValueError(f"Knowledge field {source['field']!r} is missing") from exc
                value = re.sub(r"<rt\b[^>]*>.*?</rt>|\[sound:[^\]]*\]", "", value, flags=re.S | re.I)
                words.add(html.unescape(re.sub(r"<[^>]+>", "", value)).strip())
    return words
