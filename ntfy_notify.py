"""Push na self-host ntfy (nie ntfy.sh). Pusty NTFY_URL = no-op."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

import guardian_config as gc

log = logging.getLogger("guardian")

_EMPTY_URL_WARNED = False
HTTP_TIMEOUT_S = 2.0


def ntfy_configured() -> bool:
    return bool(gc.NTFY_URL)


def _parse_topic_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    topic = parsed.path.strip("/")
    if not topic or "/" in topic:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, topic


def send_ntfy(
    title: str,
    message: str,
    *,
    priority: int = 4,
    tags: list[str] | None = None,
) -> bool:
    """POST JSON na NTFY_URL. False przy braku konfiguracji albo błędzie (nie rzuca)."""
    global _EMPTY_URL_WARNED
    url = (gc.NTFY_URL or "").strip().rstrip("/")
    if not url:
        if not _EMPTY_URL_WARNED:
            log.warning("NTFY_URL puste — alarmy tylko na dashboardzie")
            _EMPTY_URL_WARNED = True
        return False
    parsed = _parse_topic_url(url)
    if parsed is None:
        log.warning("NTFY_URL niepoprawny (oczekuję https://host/topic): %s", url)
        return False
    base, topic = parsed
    headers: dict[str, str] = {}
    token = (gc.NTFY_TOKEN or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": int(priority),
        "tags": tags or ["rotating_light", "warning"],
    }
    try:
        r = httpx.post(base, json=payload, headers=headers, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
    except Exception as e:
        log.warning("ntfy POST failed: %s", e)
        return False
    return True
