"""Unit and live integration tests for mempalace_client.py."""

import asyncio
import os
from unittest.mock import patch

import pytest

from mempalace_client import _normalize_mcp_url, search_mempalace_mcp


class FakeResponse:
    def __init__(self, status: int, json_data: dict, text_data: str = ""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, url, **kwargs):
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class RaisingSession:
    def __init__(self, exc):
        self._exc = exc

    def post(self, url, **kwargs):
        raise self._exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_normalize_mcp_url():
    assert _normalize_mcp_url("http://127.0.0.1:8767") == "http://127.0.0.1:8767/mcp"
    assert _normalize_mcp_url("http://127.0.0.1:8767/") == "http://127.0.0.1:8767/mcp"
    assert (
        _normalize_mcp_url("http://127.0.0.1:8767/mcp") == "http://127.0.0.1:8767/mcp"
    )
    assert (
        _normalize_mcp_url("http://127.0.0.1:8767/mcp/") == "http://127.0.0.1:8767/mcp"
    )


@pytest.mark.asyncio
async def test_search_mempalace_unconfigured():
    res = await search_mempalace_mcp("test", endpoint="", token="")
    assert res["status"] == "unconfigured"
    assert "kesulitan" in res["narrative"]
    assert res["drawers"] == []


@pytest.mark.asyncio
async def test_search_mempalace_mock_success():
    inner_text = (
        '{"results": [{"wing": "docs", "room": "arch", "source_file": "a.md", '
        '"similarity": 0.88, "text": "Architecture details here."}]}'
    )
    mock_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": inner_text}]},
    }
    resp = FakeResponse(200, mock_payload)
    with patch("aiohttp.ClientSession", return_value=FakeSession(resp)):
        res = await search_mempalace_mcp(
            "arch", endpoint="http://fake:8767", token="dummy"
        )
        assert res["status"] == "ok"
        assert len(res["drawers"]) == 1
        assert res["drawers"][0]["wing"] == "docs"
        assert res["drawers"][0]["room"] == "arch"
        assert "Architecture details here." in res["drawers"][0]["text"]
        assert "Hasil pencarian ingatan" in res["narrative"]


@pytest.mark.asyncio
async def test_search_mempalace_timeout_fallback():
    with patch(
        "aiohttp.ClientSession", return_value=RaisingSession(asyncio.TimeoutError())
    ):
        res = await search_mempalace_mcp(
            "arch", endpoint="http://fake:8767", token="dummy"
        )
        assert res["status"] == "timeout"
        assert "kesulitan" in res["narrative"]
        assert res["drawers"] == []


@pytest.mark.asyncio
async def test_search_mempalace_500_fail_open():
    resp = FakeResponse(500, {}, "Internal Server Error")
    with patch("aiohttp.ClientSession", return_value=FakeSession(resp)):
        res = await search_mempalace_mcp(
            "arch", endpoint="http://fake:8767", token="dummy"
        )
        assert res["status"] == "fail_open"
        assert res["http_code"] == 500
        assert "kesulitan" in res["narrative"]
        assert res["drawers"] == []


@pytest.mark.asyncio
async def test_search_mempalace_live_against_service():
    """Live verification against local port 8767 if token is present."""
    token = None
    env_local = "/home/ubuntu/projects/shorekeeper/apps/agent/.env.local"
    if os.path.exists(env_local):
        with open(env_local) as f:
            for line in f:
                if line.startswith("MEMPALACE_MCP_HTTP_TOKEN="):
                    token = line.strip().split("=", 1)[1]

    if not token:
        pytest.skip("MEMPALACE_MCP_HTTP_TOKEN not found in .env.local")

    res = await search_mempalace_mcp(
        query="shorekeeper",
        limit=2,
        endpoint="http://127.0.0.1:8767/mcp",
        token=token,
        timeout=5.0,
    )
    assert res["status"] == "ok"
    assert len(res["drawers"]) > 0
    assert res["latency_ms"] < 5000
    assert "Hasil pencarian" in res["narrative"]
