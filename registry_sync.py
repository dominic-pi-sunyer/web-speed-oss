"""Fire-and-forget contribution of fresh page maps to the Web Speed shared registry.

WRITE-ONLY CHANNEL
──────────────────
This module only ever POSTs data *up* to the registry. It never issues GET
requests, never reads any response body, and never returns registry data to the
caller. OSS clients can contribute maps but cannot query or retrieve maps through
this channel — that access is reserved for authenticated API key holders on the
hosted server. This is a deliberate architectural boundary, not a coincidence.

When enabled (the default), every fresh page map the OSS server builds is
asynchronously sent to the hosted registry at api.getwebspeed.io. This grows
the crowdsourced cache — the next agent anywhere in the world that requests the
same URL gets an instant cache HIT instead of a live fetch.

What is shared
──────────────
Structural page data only: headings, navigation links, content links, form
field names/types/labels, tables, and text blocks. Never cookies, session
tokens, or form values. JS-rendered maps are excluded (they may contain
session-specific login state or personalised results).

Configuration
─────────────
  WEB_SPEED_REGISTRY_SYNC  "true" (default) | "false"  — master on/off switch
  WEB_SPEED_REGISTRY_URL   hosted registry base URL
                            (default: https://api.getwebspeed.io)

The contribution is best-effort: network errors are silently swallowed. The
agent's request always completes regardless of whether the ping succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger("web-speed.registry-sync")

_REGISTRY_URL: str = os.environ.get(
    "WEB_SPEED_REGISTRY_URL", "https://api.getwebspeed.io"
).rstrip("/")

_sync_raw: str = os.environ.get("WEB_SPEED_REGISTRY_SYNC", "true").strip().lower()
ENABLED: bool = _sync_raw not in ("false", "0", "no", "off", "disabled")


def contribute(page_map: dict[str, Any]) -> None:
    """Schedule a best-effort contribution. Returns immediately — fire-and-forget.

    Safe to call unconditionally — skips silently if sync is disabled, the map
    contains an error, or it was JS-rendered (session-specific content risk).
    """
    if not ENABLED:
        return
    if page_map.get("error"):
        return
    if page_map.get("js_rendered"):
        return

    try:
        asyncio.get_running_loop().create_task(_post(page_map))
    except RuntimeError:
        pass  # no running event loop — skip quietly


async def _post(page_map: dict[str, Any]) -> None:
    """POST the map to /v1/contribute. Best-effort; exceptions are swallowed.

    Write-only: the response body is explicitly discarded. This function has no
    read path — it cannot return registry data to the caller under any circumstance.
    """
    try:
        import httpx  # already a dependency via fetcher.py
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{_REGISTRY_URL}/v1/contribute",
                json=page_map,
            )
            # Explicitly discard the response body — this channel is write-only.
            # OSS clients send maps up; they never receive map data back.
            await response.aclose()
        logger.debug("contributed map for %s", page_map.get("url", "?"))
    except Exception as exc:
        logger.debug(
            "registry sync skipped for %s: %s",
            page_map.get("url", "?"), exc,
        )
