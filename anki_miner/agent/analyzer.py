"""Stable Japanese analysis contract over the existing Fugashi/UniDic stack."""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from anki_miner.models.word import select_mined_form
from anki_miner.utils.text_utils import clean_subtitle_text

from .errors import AgentMiningError
from .models import AnalysisToken, AnalyzerIdentity

_SOUND_RE = re.compile(r"\[sound:[^\]]*\]", re.IGNORECASE)
_CONTRACT_VERSION = 1


def clean_knowledge_text(value: str | bytes) -> str:
    """Clean an Anki knowledge field before tokenization."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AgentMiningError("invalid_utf8", "Knowledge text is not valid UTF-8") from exc
    if not isinstance(value, str):
        raise AgentMiningError("invalid_text", "Knowledge text must be a string or UTF-8 bytes")
    return clean_subtitle_text(_SOUND_RE.sub("", value))


@runtime_checkable
class JapaneseAnalyzer(Protocol):
    @property
    def identity(self) -> AnalyzerIdentity: ...

    def analyze(self, text: str | bytes) -> Sequence[AnalysisToken]: ...


class FugashiJapaneseAnalyzer:
    """The production analyzer used by both profile and episode contracts."""

    def __init__(self) -> None:
        from anki_miner.services.tagger import get_shared_tagger

        self._tagger = get_shared_tagger()
        try:
            version = importlib.metadata.version("unidic-lite")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - packaging fault
            version = "unknown"
        self._identity = AnalyzerIdentity(_CONTRACT_VERSION, "fugashi", f"unidic-lite:{version}")

    @property
    def identity(self) -> AnalyzerIdentity:
        return self._identity

    def analyze(self, text: str | bytes) -> Sequence[AnalysisToken]:
        cleaned = clean_knowledge_text(text)
        result: list[AnalysisToken] = []
        cursor = 0
        for node in self._tagger(cleaned):
            surface = str(node.surface)
            start = cleaned.find(surface, cursor)
            if start < 0:
                raise AgentMiningError(
                    "invalid_token_span",
                    "Fugashi returned a surface that is not present in the cleaned source text",
                    {"surface": surface},
                )
            end = start + len(surface)
            cursor = end
            feature = node.feature
            lemma = str(getattr(feature, "lemma", "") or surface)
            orth_base = str(getattr(feature, "orthBase", "") or lemma)
            reading = str(getattr(feature, "kana", "") or getattr(feature, "pron", "") or surface)
            pos = str(getattr(feature, "pos1", "") or "")
            subtype = str(getattr(feature, "pos2", "") or "")
            lexical_id = select_mined_form(pos, orth_base, lemma, surface, getattr(feature, "pron", None))
            token = AnalysisToken(surface, lexical_id, lemma, reading, pos, subtype, start, end)
            token.validate(cleaned)
            result.append(token)
        return result


class SubtitleParserJapaneseAnalyzer:
    """Analyzer facade that emits the exact identities used by episode parsing."""

    def __init__(self, parser: Any) -> None:
        self._parser = parser
        try:
            version = importlib.metadata.version("unidic-lite")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - packaging fault
            version = "unknown"
        self._identity = AnalyzerIdentity(_CONTRACT_VERSION, "fugashi-subtitle-parser", f"unidic-lite:{version}")

    @property
    def identity(self) -> AnalyzerIdentity:
        return self._identity

    def analyze(self, text: str | bytes) -> Sequence[AnalysisToken]:
        from anki_miner.models.reading import ReadingUnit

        cleaned = clean_knowledge_text(text)
        words, _line_index, _counts = self._parser.parse_text_units(
            [ReadingUnit(cleaned, 0, "profile")],
            False,
        )
        result: list[AnalysisToken] = []
        for word in words:
            start = word.surface_start
            end = word.surface_end
            if start < 0 or end <= start:
                start = cleaned.find(word.surface)
                end = start + len(word.surface)
            token = AnalysisToken(
                word.surface,
                word.mined_form,
                word.lemma,
                word.expression_reading or word.reading,
                word.pos or "",
                "",
                start,
                end,
            )
            token.validate(cleaned)
            result.append(token)
        return result


def validate_analyzer_output(analyzer: JapaneseAnalyzer, text: str | bytes) -> tuple[str, Sequence[AnalysisToken]]:
    cleaned = clean_knowledge_text(text)
    tokens = analyzer.analyze(cleaned)
    for token in tokens:
        token.validate(cleaned)
    return cleaned, tokens
