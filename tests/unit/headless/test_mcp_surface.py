from __future__ import annotations

import asyncio

import pytest

from anki_miner.mcp_server.server import create_server


class FakeApplication:
    def sync_learner_profile(self):
        return {}

    def prepare_mining_batch(self, request):
        return request

    def list_mining_candidates(self, *args, **kwargs):
        return {}

    def commit_mining_selection(self, request):
        return request

    def get_mining_job(self, job_id):
        return {"job_id": job_id}


def test_mcp_exposes_only_the_five_orchestration_tools():
    pytest.importorskip("mcp")
    server = create_server(FakeApplication())
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == [
        "sync_learner_profile",
        "prepare_mining_batch",
        "list_mining_candidates",
        "commit_mining_selection",
        "get_mining_job",
    ]
