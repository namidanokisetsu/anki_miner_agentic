from __future__ import annotations

import pytest

from anki_miner.agent.analyzer import clean_knowledge_text
from anki_miner.agent.errors import AgentMiningError
from anki_miner.agent.models import AnalysisToken


def test_cleaning_removes_anki_markup_before_analysis():
    assert clean_knowledge_text("<b> 食べる </b>&nbsp;[sound:a.mp3]") == "食べる"


def test_invalid_utf8_is_rejected():
    with pytest.raises(AgentMiningError) as raised:
        clean_knowledge_text(b"\xff")
    assert raised.value.code == "invalid_utf8"


def test_invalid_token_span_is_rejected():
    token = AnalysisToken("違う", "違う", "違う", "ちがう", "名詞", "", 0, 2)
    with pytest.raises(AgentMiningError) as raised:
        token.validate("食事")
    assert raised.value.code == "invalid_token_span"
