"""Connections the core provides to plugins, and secret resolution."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import httpx

USER_AGENT = "watchkeep/0.1"


class Unreachable(Exception):
    """The target didn't answer (timeout, DNS, refused). For monitors this is data, not a bug."""


class HttpConnection:
    """A thin HTTP client with sane timeouts. Plugins add their own auth headers."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    def get_json(self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def post_json(self, url: str, *, headers: dict[str, str] | None = None, json: Any = None) -> Any:
        """POST a JSON body and parse the JSON answer (e.g. a GraphQL query). Raises on 4xx/5xx."""
        response = self._client.post(url, headers=headers, json=json)
        response.raise_for_status()
        return response.json()

    def get(self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET following redirects, for bodies that aren't JSON (XML, HTML).

        Raises httpx.HTTPStatusError on 4xx/5xx. Other statuses come back as is,
        so a 304 answering If-None-Match / If-Modified-Since is not an error.
        """
        response = self._client.get(url, headers=headers, params=params, follow_redirects=True)
        if response.status_code >= 400:
            response.raise_for_status()
        return response

    def probe(self, url: str, *, timeout: float) -> tuple[int, int]:
        """GET without following redirects. Returns (status, response_ms) or raises Unreachable."""
        started = time.perf_counter()
        try:
            response = self._client.get(url, timeout=timeout, follow_redirects=False)
        except httpx.TimeoutException:
            raise Unreachable(f"timeout after {timeout:g}s") from None
        except httpx.TransportError as exc:
            raise Unreachable(f"unreachable: {type(exc).__name__}") from None
        return response.status_code, int((time.perf_counter() - started) * 1000)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


REFERENCE_PREFIXES = ("gh:", "env:")


def is_reference(value: str) -> bool:
    """A reference names where a secret lives, so storing it reveals nothing."""
    return value.startswith(REFERENCE_PREFIXES)


def resolve_secret(value: str) -> str:
    """Turn a secret reference into its value.

    "gh:<user>"  -> token of that account from the GitHub CLI
    "env:<VAR>"  -> environment variable
    anything else is used as-is.
    """
    if value.startswith("gh:"):
        user = value[3:]
        result = subprocess.run(
            ["gh", "auth", "token", "--user", user], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh has no account {user!r} (logged out?). Press d to remove, or run gh auth login")
        return result.stdout.strip()
    if value.startswith("env:"):
        var = value[4:]
        if var not in os.environ:
            raise RuntimeError(f"environment variable {var} is not set")
        return os.environ[var]
    return value


CONNECTIONS = {"http": HttpConnection}
