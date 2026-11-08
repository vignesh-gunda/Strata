"""Tiny JSON-over-HTTPS transport with retry, shared by both clients.

Retries only 429 and 5xx (and network errors), with exponential backoff.
The transport and sleep are injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# (url, headers, body, timeout) -> (status, parsed JSON body)
Transport = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, dict[str, Any]]]

RETRYABLE = {429, 500, 502, 503, 504}


def urllib_transport(url: str, headers: dict[str, str], body: dict[str, Any],
                     timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except Exception:
            payload = {"error": {"message": "non-JSON error body"}}
        return error.code, payload


def post_with_retry(
    url: str,
    key: str,
    body: dict[str, Any],
    *,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = 3,
    backoff: float = 1.0,
    timeout: float = 120.0,
) -> tuple[int, dict[str, Any]]:
    """POST with a bearer key. Returns the last (status, payload)."""
    headers = {"Authorization": f"Bearer {key}"}
    status, payload = 0, {}
    for attempt in range(retries + 1):
        try:
            status, payload = transport(url, headers, body, timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            status, payload = 0, {"error": {"message": f"network error: {type(error).__name__}"}}
        if status != 0 and status not in RETRYABLE:
            return status, payload
        if attempt < retries:
            sleep(backoff * (2 ** attempt))
    return status, payload
