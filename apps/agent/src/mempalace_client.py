"""MemPalace L2 MCP HTTP Client Connector.

Provides robust async querying to MemPalace L2 via MCP JSON-RPC 2.0 over HTTP.
Designed for voice agents (low latency, fail-open graceful fallbacks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


def _normalize_mcp_url(endpoint: str) -> str:
    """Normalize endpoint to point to /mcp path."""
    url = endpoint.strip().rstrip("/")
    if not url.endswith("/mcp"):
        url = f"{url}/mcp"
    return url


async def search_mempalace_mcp(
    query: str,
    limit: int = 3,
    wing: str | None = None,
    room: str | None = None,
    timeout: float = 1.5,
    endpoint: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Search MemPalace L2 using MCP JSON-RPC 2.0 protocol over HTTP.

    Returns a dict with:
        - status: "ok" | "empty" | "error" | "timeout" | "fail_open" | "unconfigured"
        - narrative: Human-friendly Indonesian text suitable for voice output
        - drawers: List of parsed drawer dicts (wing, room, source_file, similarity, text, snippet)
        - latency_ms: Execution time in milliseconds
        - raw: Raw decoded results (if available)

    Fails open gracefully: returns polite natural narrative on timeout or error,
    never crashing or leaking raw stack traces.
    """
    mcp_endpoint = (
        os.getenv("MEMPALACE_MCP_HTTP_ENDPOINT", "") if endpoint is None else endpoint
    )
    mcp_token = os.getenv("MEMPALACE_MCP_HTTP_TOKEN", "") if token is None else token

    if not mcp_endpoint or not mcp_token:
        return {
            "status": "unconfigured",
            "narrative": "Aku sedang kesulitan mengakses memori jangka panjangku — konfigurasi MCP belum tersedia.",
            "drawers": [],
            "latency_ms": 0.0,
        }

    url = _normalize_mcp_url(mcp_endpoint)
    req_id = int(time.time() * 1000) % 1000000

    arguments: dict[str, Any] = {
        "query": query,
        "limit": limit,
    }
    if wing:
        arguments["wing"] = wing
    if room:
        arguments["room"] = room

    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": "mempalace_search",
            "arguments": arguments,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {mcp_token}",
    }

    t0 = time.perf_counter()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp,
        ):
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if resp.status != 200:
                logger.warning(
                    "MemPalace HTTP status %s: %s", resp.status, await resp.text()
                )
                return {
                    "status": "fail_open",
                    "http_code": resp.status,
                    "latency_ms": elapsed_ms,
                    "narrative": "Aku sedang kesulitan mengakses ingatanku, coba lagi sebentar lagi.",
                    "drawers": [],
                }

            data = await resp.json()
            if "error" in data:
                err_info = data.get("error", {})
                logger.warning("MemPalace JSON-RPC error: %s", err_info)
                return {
                    "status": "error",
                    "error": err_info,
                    "latency_ms": elapsed_ms,
                    "narrative": "Aku sedang kesulitan mengakses ingatanku, coba lagi sebentar lagi.",
                    "drawers": [],
                }

            # Parse MCP response envelope: result.content[0].text is a JSON string
            content_items = data.get("result", {}).get("content", [])
            raw_text = content_items[0].get("text", "{}") if content_items else "{}"

            try:
                search_data = json.loads(raw_text)
            except Exception as parse_err:
                logger.warning("Failed to parse inner MCP JSON text: %s", parse_err)
                return {
                    "status": "error",
                    "error": str(parse_err),
                    "latency_ms": elapsed_ms,
                    "narrative": "Aku sedang kesulitan mengakses ingatanku, coba lagi sebentar lagi.",
                    "drawers": [],
                }

            results = search_data.get("results", [])
            if not results:
                return {
                    "status": "empty",
                    "latency_ms": elapsed_ms,
                    "narrative": f"Tidak ditemukan ingatan terkait '{query}' dalam memoriku.",
                    "drawers": [],
                }

            drawers = []
            narrative_items = []
            for r in results[:limit]:
                text = r.get("text", "")
                wing_name = r.get("wing", "unknown")
                room_name = r.get("room", "unknown")
                source_file = r.get("source_file", "?")
                similarity = r.get("similarity", 0.0)

                # Clean preview snippet
                snippet = text.strip()
                if len(snippet) > 150:
                    snippet = snippet[:150] + "..."

                drawers.append(
                    {
                        "wing": wing_name,
                        "room": room_name,
                        "source_file": source_file,
                        "similarity": similarity,
                        "text": text,
                        "snippet": snippet,
                    }
                )

                narrative_items.append(f"- ({wing_name}/{room_name}): {snippet}")

            narrative = f"Hasil pencarian ingatan untuk '{query}':\n" + "\n".join(
                narrative_items
            )

            return {
                "status": "ok",
                "latency_ms": elapsed_ms,
                "narrative": narrative,
                "drawers": drawers,
                "raw": search_data,
            }

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.warning("MemPalace search timeout after %.1fms: %s", elapsed_ms, query)
        return {
            "status": "timeout",
            "latency_ms": elapsed_ms,
            "narrative": "Aku sedang kesulitan mengakses ingatanku, coba lagi sebentar lagi.",
            "drawers": [],
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.warning("MemPalace search exception after %.1fms: %s", elapsed_ms, exc)
        return {
            "status": "exception",
            "error": str(exc),
            "latency_ms": elapsed_ms,
            "narrative": "Aku sedang kesulitan mengakses ingatanku, coba lagi sebentar lagi.",
            "drawers": [],
        }
