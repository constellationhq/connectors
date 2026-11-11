"""The Constellation API over HTTP: auth, retries and workflow results read whole and in order.

Operations generated from the contract's openapi.json belong on Client as thin methods over call();
what is handwritten here is only what every operation shares. Standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from constellation_space import results

DEFAULT_URL = "https://api.constellation.space"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# The longest Retry-After honored: a longer one is waited this long, then asked again.
MAX_RETRY_AFTER = 60.0


class ApiError(Exception):
    """Constellation answered `status` with `body`, and retrying did not or cannot help."""

    def __init__(self, status: int, body: bytes, api_url: str | None = None) -> None:
        super().__init__(
            results.refusal(status, body, api_url=api_url)
            or f"Constellation answered {status}: {body[:300]!r}"
        )
        self.status = status
        self.body = body
        self.api_url = api_url

    @property
    def refusal(self) -> str | None:
        """Why the key or plan was refused and the fix, for a 401 or 403; None otherwise."""
        return results.refusal(self.status, self.body, api_url=self.api_url)


class Client:
    """`call()` is the one way out: it sends the key as x-api-key (never over plain http to
    another machine), retries a 429, a 5xx or a dropped connection `retries` times with
    exponential backoff (honoring Retry-After up to MAX_RETRY_AFTER), and raises ApiError for any other failure."""

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 60,
    ) -> None:
        self.api_url = (api_url or os.environ.get("CONSTELLATION_API_URL") or DEFAULT_URL).rstrip(
            "/"
        )
        self.api_key = (api_key or os.environ.get("CONSTELLATION_API_KEY") or "").strip()
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """One API call; the answer's JSON, or None for an empty one."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if why := results.key_refusal(self.api_url):
                raise ValueError(why)
            headers["x-api-key"] = self.api_key
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                f"{self.api_url}{path}", data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                answer = exc.read()
                if exc.code not in RETRY_STATUSES or attempt == self.retries:
                    raise ApiError(exc.code, answer, self.api_url) from None
                wait = _retry_after(exc.headers.get("Retry-After"))
                if wait is not None:
                    print(
                        f"constellation: answered {exc.code}; retrying in {wait:g} s (Retry-After)",
                        file=sys.stderr,
                        flush=True,
                    )
            except OSError:  # unreachable, timed out, or the connection dropped
                if attempt == self.retries:
                    raise
                wait = None
            time.sleep(wait if wait is not None else self.backoff * 2**attempt)
        raise AssertionError("unreachable")

    def results(self, consumer: str, after: str = "") -> Iterator[dict[str, Any]]:
        """Every run after cursor `after` for connection `consumer`, oldest first, each joined
        whole across its pages; stops at the newest result. Save each entry's cursor once it
        is applied and pass it back as `after` to resume. A cursor older than the oldest entry
        the feed holds is logged: the runs between them expired unread."""
        while True:
            page = results.results_page(self.call, consumer, after)
            if lost := results.expired(page, after):
                print(f"constellation: {lost}", file=sys.stderr, flush=True)
            if not page["entries"]:
                return
            for entry in page["entries"]:
                yield results.whole(entry, lambda c, n: results.rest_of_run(self.call, consumer, c, n))
                after = entry["cursor"]


def _retry_after(value: str | None) -> float | None:
    try:
        return min(max(0.0, float(value)), MAX_RETRY_AFTER) if value else None
    except ValueError:
        return None
