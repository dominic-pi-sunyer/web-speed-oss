"""On-disk cache for structured page maps.

Layout: ./cache/<sha256(url)[:32]>.json
Each file: {"url", "cached_at" (ISO timestamp), "map"}.
Entries older than TTL_SECONDS are stale and treated as misses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_DIR = Path(__file__).resolve().parent / "cache"
TTL_SECONDS = 24 * 60 * 60


def _ensure_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _path_for(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{digest}.json"


def get(url: str) -> dict[str, Any] | None:
    """Return the cached map for `url`, or None if missing/stale/corrupt."""
    path = _path_for(url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    cached_at_raw = payload.get("cached_at")
    if not isinstance(cached_at_raw, str):
        return None
    try:
        cached_at = datetime.fromisoformat(cached_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - cached_at).total_seconds()
    if age > TTL_SECONDS:
        return None

    cached_map = payload.get("map")
    if not isinstance(cached_map, dict):
        return None
    return cached_map


def put(url: str, page_map: dict[str, Any]) -> None:
    """Write `page_map` to the cache atomically (tmp + rename)."""
    _ensure_dir()
    path = _path_for(url)
    payload = {
        "url": url,
        "cached_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "map": page_map,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def invalidate(url: str) -> bool:
    """Delete the cache entry for `url`. Returns True if a file was removed."""
    path = _path_for(url)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
