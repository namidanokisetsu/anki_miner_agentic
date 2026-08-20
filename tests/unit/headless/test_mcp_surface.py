from __future__ import annotations

import asyncio

import pytest

from anki_miner.mcp_server.server import create_server


class FakeApplication:
    def prepare_mining_run(self, request):
        return request

    def commit_mining_run(self, run_id, reviews):
        return {"run_id": run_id, "reviews": reviews}


def test_mcp_exposes_only_the_two_call_workflow():
    pytest.importorskip("mcp")
    server = create_server(FakeApplication())
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == ["prepare_mining_run", "commit_mining_run"]
    assert "explicitly stated or accepted by the user" in tools[0].description
