import json
import os
import sys
from pathlib import Path
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Add token-server directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from token_server import create_app

@pytest.mark.asyncio
async def test_unauthorized_token_request_when_secret_set(monkeypatch):
    monkeypatch.setenv("SHOREKEEPER_TOKEN_SECRET", "super-secret-token-key")
    monkeypatch.setenv("SHOREKEEPER_REQUIRE_AUTH", "1")
    
    async with TestClient(TestServer(create_app())) as client:
        resp = await client.get("/token?identity=schnee&room=room-1")
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "UNAUTHORIZED"

@pytest.mark.asyncio
async def test_authorized_bearer_token(monkeypatch):
    secret = "super-secret-token-key"
    monkeypatch.setenv("SHOREKEEPER_TOKEN_SECRET", secret)
    monkeypatch.setenv("SHOREKEEPER_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret01234567890123456789012345678901")
    
    async with TestClient(TestServer(create_app())) as client:
        headers = {"Authorization": f"Bearer {secret}"}
        resp = await client.get("/token?identity=schnee&room=room-1", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert "token" in data

@pytest.mark.asyncio
async def test_authorized_query_param_token(monkeypatch):
    secret = "super-secret-token-key"
    monkeypatch.setenv("SHOREKEEPER_TOKEN_SECRET", secret)
    monkeypatch.setenv("SHOREKEEPER_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret01234567890123456789012345678901")
    
    async with TestClient(TestServer(create_app())) as client:
        resp = await client.get(f"/token?identity=schnee&room=room-1&auth_token={secret}")
        assert resp.status == 200
        data = await resp.json()
        assert "token" in data

@pytest.mark.asyncio
async def test_identity_spoofing_guard(monkeypatch):
    secret = "secret"
    monkeypatch.setenv("SHOREKEEPER_TOKEN_SECRET", secret)
    monkeypatch.setenv("SHOREKEEPER_REQUIRE_AUTH", "1")
    
    async with TestClient(TestServer(create_app())) as client:
        headers = {"Authorization": f"Bearer {secret}"}
        # Malformed / dangerous identity
        resp = await client.get("/token?identity=../../root&room=room-1", headers=headers)
        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "INVALID_IDENTITY"
