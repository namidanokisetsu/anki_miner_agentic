"""Small Japanese shortlist; upstream remains responsible for final mining."""

import math
import unicodedata
from collections import Counter
from pathlib import Path


def discover(sources, known, limit):
    import fugashi
    import pysubs2

    tagger = fugashi.Tagger()
    known = {unicodedata.normalize("NFC", word) for word in known}
    counts = Counter()
    examples = {}
    for source_index, source in enumerate(sources):
        path = Path(source["subtitle_file"])
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f"Subtitle file exceeds 16 MiB: {path}")
        subtitles = pysubs2.load(str(path))
        if len(subtitles) > 50_000:
            raise ValueError(f"Subtitle file exceeds 50,000 cues: {path}")
        for cue in subtitles:
            if cue.is_comment:
                continue
            if not math.isfinite(cue.start) or cue.start < 0 or cue.start >= cue.end:
                raise ValueError(f"Invalid subtitle timing at {cue.start} ms in {path}")
            sentence = " ".join(cue.plaintext.split())
            if not sentence or len(sentence) > 500:
                continue
            for token in tagger(sentence):
                feature = token.feature
                if feature.pos1 not in {"名詞", "動詞", "形容詞", "副詞", "形状詞"}:
                    continue
                if feature.pos2 in {"固有名詞", "数詞", "非自立可能"}:
                    continue
                word = token.surface
                if feature.pos1 in {"動詞", "形容詞"}:
                    word = feature.orthBase or token.surface
                word = unicodedata.normalize("NFC", word)
                if word in known or not word.strip() or len(word) > 100:
                    continue
                counts[word] += 1
                example = {
                    "word": word,
                    "sentence": sentence,
                    "source": source_index,
                    "line_start": cue.start / 1000,
                }
                # Prefer a concise complete example; frequency is counted over all cues.
                if word not in examples or len(sentence) < len(examples[word]["sentence"]):
                    examples[word] = example
    ranked = sorted(examples, key=lambda word: (-counts[word], len(examples[word]["sentence"]), word))
    return [
        {"id": f"c{index + 1:04}", **examples[word], "occurrences": counts[word]}
        for index, word in enumerate(ranked[:limit])
    ]
