"""tools/contract/dev_feed.py, the nightly job's read of a live feed, against the in-process API."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from api import ResultsApi, item, output, to_otlp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_feed", ROOT / "tools/contract/dev_feed.py")
dev_feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_feed)

PASS = {"type": "pass", "id": "p"}


def run(cursor: str, *items: dict) -> dict:
    otlp = to_otlp(
        "t",
        list(items),
        datetime(2030, 1, 1, tzinfo=UTC),
        {"workflow": "w", "run_id": cursor},
        output("my-bridge"),
    )
    return {"cursor": cursor, "otlp": otlp}


def test_a_feed_the_bridges_read_has_no_problems_and_names_no_consumer():
    api = ResultsApi([run("1#a", item(PASS, "2030-01-01T01:00:00+00:00", {"p50": 1.0}))])
    assert dev_feed.problems(api) == []
    assert all("consumer" not in q for _, _, q in api.calls)


def test_an_empty_feed_has_no_problems():
    assert dev_feed.problems(ResultsApi([])) == []


def test_a_malformed_item_and_a_newer_item_shape_are_problems():
    broken = run("1#a", item(PASS, "2030-01-01T01:00:00+00:00", {"p50": 1.0}))
    broken["otlp"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] = {
        "stringValue": "x"
    }
    assert dev_feed.problems(ResultsApi([broken])) == [
        "item 0 of run 1#a is malformed: ValueError('no entity with an id and a type')"
    ]
    newer = run("1#a", item(PASS, "2030-01-01T01:00:00+00:00", {"p50": 1.0}))
    for record in newer["otlp"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
        record["attributes"][0]["value"] = {"stringValue": "constellation.item/v3"}
    assert dev_feed.problems(ResultsApi([newer])) == [
        "run 1#a has items {'constellation.item/v3'}; the bridges read constellation.item/v2"
    ]


def test_from_latest_answered_with_entries_is_a_problem():
    class Old(ResultsApi):
        def call(self, method, path, body=None):
            return {"entries": [], "next": "x"} if "from=latest" not in path else {"entries": [1]}

    assert dev_feed.problems(Old([])) == [
        "from=latest answered {'entries': [1]}: a first start needs no entries and a next cursor"
    ]
