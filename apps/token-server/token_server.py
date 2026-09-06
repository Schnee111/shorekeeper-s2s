"""token_server.py — Shorekeeper token & voice registry server.

Self-contained copy (bukan modifikasi repo jarvis-livekit) untuk project
Shorekeeper. Bedanya dengan versi jarvis lama:
  - Default port 8083 (jarvis lama pakai 8082 — tidak bentrok)
  - Voice registry = 30 suara native Gemini Live (bukan Fish Audio)
  - Agent dispatch = agent_name "shorekeeper" (bukan "jarvis")

Env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
"""

import hmac
import json
import os
import re

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

load_dotenv()

PORT = int(os.getenv("SHOREKEEPER_TOKEN_PORT", "8083"))
AGENT_NAME = os.getenv("SHOREKEEPER_AGENT_NAME", "shorekeeper")
TOKEN_SECRET = os.getenv("SHOREKEEPER_TOKEN_SECRET", "")
REQUIRE_AUTH = os.getenv("SHOREKEEPER_REQUIRE_AUTH", "1" if TOKEN_SECRET else "0") == "1"
IDENTITY_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

# 30 suara native Gemini Live (sama dengan VALID_GEMINI_VOICES di agent).
VOICE_LABELS = {
    "Aoede": "Warm · Melodic",
    "Achernar": "Bright · Resonant",
    "Achird": "Crisp · Balanced",
    "Algenib": "Calm · Grounded",
    "Algieba": "Warm · Steady",
    "Alnilam": "Deep · Dynamic",
    "Autonoe": "Gentle · Expressive",
    "Callirrhoe": "Smooth · Radiant",
    "Charon": "Deep · Authoritative",
    "Despina": "Smooth · Conversational",
    "Enceladus": "Light · Cheerful",
    "Erinome": "Polished · Clear",
    "Fenrir": "Direct · Strong",
    "Gacrux": "Mature · Grounded",
    "Iapetus": "Rich · Steady",
    "Kore": "Calm · Clear",
    "Laomedeia": "Soft · Airy",
    "Leda": "Gentle · Soothing",
    "Orus": "Bold · Confident",
    "Puck": "Playful · Energetic",
    "Pulcherrima": "Vibrant · Melodic",
    "Rasalgethi": "Warm · Deep",
    "Sadachbia": "Focused · Direct",
    "Sadaltager": "Quiet · Refined",
    "Schedar": "Firm · Resonant",
    "Sulafat": "Gentle · Harmonic",
    "Umbriel": "Subtle · Calm",
    "Vindemiatrix": "Clear · Eloquent",
    "Zephyr": "Bright · Expressive",
    "Zubenelgenubi": "Deep · Classic",
}
DEFAULT_VOICE = "Aoede"


async def voices_list(_request: web.Request) -> web.Response:
    voices = [
        {"id": vid, "label": vid, "desc": label, "default": vid == DEFAULT_VOICE}
        for vid, label in VOICE_LABELS.items()
    ]
    return web.Response(
        text=json.dumps({"voices": voices}),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def check_auth(request: web.Request) -> bool:
    secret = os.getenv("SHOREKEEPER_TOKEN_SECRET", TOKEN_SECRET)
    require_auth = os.getenv("SHOREKEEPER_REQUIRE_AUTH", "1" if secret else "0") == "1"
    if not require_auth and not secret:
        return True
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if secret and hmac.compare_digest(token, secret):
            return True
    token_param = request.query.get("auth_token", "")
    if token_param and secret and hmac.compare_digest(token_param, secret):
        return True
    return False


async def get_token(request: web.Request) -> web.Response:
    if not check_auth(request):
        return web.Response(
            status=401,
            text=json.dumps({"error": "UNAUTHORIZED", "message": "Valid Bearer token or auth_token required"}),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    room = request.query.get("room", "shorekeeper-main")
    identity = request.query.get("identity", "schnee")
    if not IDENTITY_REGEX.match(identity):
        return web.Response(
            status=400,
            text=json.dumps({"error": "INVALID_IDENTITY", "message": "Identity must match ^[a-zA-Z0-9_-]{1,32}$"}),
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    voice_id = request.query.get("voice") or DEFAULT_VOICE
    if voice_id not in VOICE_LABELS:
        voice_id = DEFAULT_VOICE
    model = request.query.get("model", "")
    attributes = {"voice": voice_id}
    if model:
        attributes["model"] = model

    # Agent auto-dispatch: participant masuk room → LiveKit dispatch agent.
    # Pola livekit-api 1.2.x: RoomConfiguration + RoomAgentDispatch
    # (bukan VideoGrants.agent_dispatches — field itu baru di versi lebih baru).
    room_config = api.RoomConfiguration(
        agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
    )

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
    )

    token = (
        api.AccessToken(
            os.getenv("LIVEKIT_API_KEY"),
            os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .with_room_config(room_config)
        .with_attributes(attributes)
        .to_jwt()
    )

    return web.Response(
        text=json.dumps({"token": token, "voice_id": voice_id}),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def _cors_options(_request: web.Request) -> web.Response:
    return web.Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    )


def create_app() -> web.Application:
    new_app = web.Application()
    new_app.router.add_get("/token", get_token)
    new_app.router.add_get("/voices", voices_list)
    new_app.router.add_options("/token", _cors_options)
    return new_app


app = create_app()

if __name__ == "__main__":
    print(f"Shorekeeper token server starting on :{PORT} (agent: {AGENT_NAME})")
    web.run_app(app, port=PORT, host="127.0.0.1")
