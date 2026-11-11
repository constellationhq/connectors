"""A live API's feed against what the bridges read: its newest run, read with the client they use.

    CONSTELLATION_API_URL=<api> CONSTELLATION_API_KEY=<key> python3 tools/contract/dev_feed.py

Read-only: GETs only, and no consumer is named, so the API records no pull. Exits non-zero with
what does not match: from=latest not answered as a first start needs, a run in an item shape the
bridges do not read, or an item they would skip as malformed. The nightly contract job runs it
against the dev API.
"""

import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "python" / "src"))

from constellation_space import results  # noqa: E402
from constellation_space.client import Client  # noqa: E402


def feed(api: Client, **query: Any) -> dict[str, Any]:
    return api.call("GET", f"/predictions/feed?{urllib.parse.urlencode(query)}")


def problems(api: Client) -> list[str]:
    """How the newest run on `api`'s feed differs from what the bridges read; empty when it
    does not, or when the feed holds no run."""
    start = feed(api, **{"from": "latest", "after": ""})
    if start.get("entries") != [] or not isinstance(start.get("next"), str):
        return [f"from=latest answered {start!r}: a first start needs no entries and a next cursor"]
    page = feed(api, after=start["next"], limit=1, page_records=results.PAGE_RECORDS)
    found = []
    for entry in page["entries"]:
        records = [
            r
            for logs in entry["otlp"]["resourceLogs"]
            for scope in logs["scopeLogs"]
            for r in scope["logRecords"]
        ]
        if newer := results.newer_shapes(records):
            found.append(
                f"run {entry['cursor']} has items {newer}; the bridges read {results.SCHEMA}"
            )
            continue
        for n, record in enumerate(records):
            try:
                results.check(results.plain(record["body"]))
            except results.MALFORMED as exc:
                found.append(f"item {n} of run {entry['cursor']} is malformed: {exc!r}")
    return found


if __name__ == "__main__":
    api = Client()
    if found := problems(api):
        sys.exit(f"{api.api_url}'s feed is not what the bridges read:\n  " + "\n  ".join(found))
    print(f"{api.api_url}'s feed is what the bridges read")
