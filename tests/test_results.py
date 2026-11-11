"""The workflow results protocol every bridge here shares (constellation_space.results)."""

import json
from datetime import UTC, datetime, timezone

import api
import pytest
from constellation_space import results


def test_the_bridges_read_the_item_shape_the_api_publishes():
    assert results.SCHEMA == api.SCHEMA_VERSION


def test_a_paged_run_is_joined_whole_before_it_is_applied():
    """The API pages a large run for a bridge that asks; the bridge fetches every page and joins
    each resource's records before applying, so it never takes a part for the whole run."""
    resource = {"attributes": [{"key": "constellation.connection", "value": {"stringValue": "x"}}]}
    other = {"attributes": [{"key": "constellation.connection", "value": {"stringValue": "y"}}]}

    def logs(res, *ns):
        return {
            "resource": res,
            "scopeLogs": [{"scope": {"name": "s"}, "logRecords": [{"n": n} for n in ns]}],
        }

    pages = {
        1: {"cursor": "c#r", "otlp": {"resourceLogs": [logs(resource, 3, 4)]}, "next_page": 2},
        2: {"cursor": "c#r", "otlp": {"resourceLogs": [logs(resource, 5), logs(other, 6)]}},
    }
    asked = []

    def fetch(cursor, page):
        asked.append((cursor, page))
        return pages[page]

    first = {"cursor": "c#r", "otlp": {"resourceLogs": [logs(resource, 1, 2)]}, "next_page": 1}
    joined = results.whole(first, fetch)
    assert asked == [("c#r", 1), ("c#r", 2)] and "next_page" not in joined
    assert [
        [r["n"] for r in rl["scopeLogs"][0]["logRecords"]] for rl in joined["otlp"]["resourceLogs"]
    ] == [
        [1, 2, 3, 4, 5],
        [6],
    ]


PLAN = json.dumps(
    {"detail": {"error": "plan_required", "plan": "enterprise", "feature": "output:lattice"}}
).encode()


def test_a_plan_refusal_says_what_the_plan_lacks_and_where_to_upgrade():
    why = results.refusal(403, PLAN)
    assert why and "plan does not include output:lattice" in why
    assert "the enterprise plan" in why and "https://platform.constellation.space/billing" in why


@pytest.mark.parametrize(
    "status, body, says",
    [
        (401, b"", "HTTP 401"),
        (403, b'{"detail": {"error": "insufficient_scope", "required": "x:read"}}', "lacks x:read"),
        (403, b'{"detail": {"error": "live_key_required"}}', "test key"),
        (403, b"<html>Forbidden</html>", "HTTP 403"),
    ],
)
def test_a_refused_key_says_how_to_get_one_that_works(status, body, says):
    why = results.refusal(status, body, needs="a:read and b:read")
    assert (
        why
        and says in why
        and "a key with a:read and b:read" in why
        and "https://platform.constellation.space/keys" in why
    )


@pytest.mark.parametrize(
    "api, keys",
    [
        ("https://api.constellation.space", "https://platform.constellation.space/keys"),
        ("https://dev.api.constellation.space/", "https://dev-platform.constellation.space/keys"),
        (
            "https://stage.api.constellation.space",
            "https://stage-platform.constellation.space/keys",
        ),
        (
            "https://s-omeed.api.constellation.space",
            "https://platform.s-omeed.constellation.space/keys",
        ),
        ("http://127.0.0.1:7070", "https://platform.constellation.space/keys"),
    ],
)
def test_a_refusal_links_the_keys_page_of_the_api_the_bridge_uses(monkeypatch, api, keys):
    assert keys in results.refusal(401, b"", api_url=api)
    monkeypatch.setenv("CONSTELLATION_API_URL", api)
    assert keys in results.refusal(401, b"")
    billing = keys.replace("/keys", "/billing")
    assert billing in results.refusal(403, PLAN)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_only_401_and_403_are_refusals(status):
    assert results.refusal(status, PLAN) is None


SWAP_ACTION = {"verb": "swap", "from": "p1", "to": "p2", "deadline": "2030-01-01T00:00:00+00:00"}


@pytest.mark.parametrize(
    ("facets", "state"),
    [
        ({}, None),
        ({"action": {**SWAP_ACTION, "mode": "suggest"}}, "advice"),
        ({"action": {**SWAP_ACTION, "mode": "approve"}, "action_status": "approved"}, "approved"),
        ({"action_status": "pending"}, "pending"),
        ({"action_status": "rejected"}, "rejected"),
        # A status this bridge does not know is held, never a plain forecast.
        ({"action_status": "expired"}, "pending"),
        # Approved with nothing to carry out: held, never a plain forecast.
        ({"action_status": "approved"}, "pending"),
    ],
)
def test_an_items_action_state_says_whether_it_is_advice_an_instruction_or_held(facets, state):
    item = {"entity": {"type": "pass", "id": "p1"}, "target_at": "2030-01-01T01:00:00+00:00"}
    assert results.action_state({**item, **facets}) == state


def approved(to: str = "q", decision: str | None = "d1", year: int = 2099) -> dict:
    """An approved swap of pass p to `to`, whose pass ends at 01:00 on New Year's Day `year`."""
    item = {
        "entity": {"type": "pass", "id": "p"},
        "target_at": f"{year}-01-01T00:00:00+00:00",
        "until": f"{year}-01-01T01:00:00+00:00",
        "action": {
            "verb": "swap",
            "mode": "approve",
            "to": to,
            "deadline": f"{year - 1}-12-31T23:00:00+00:00",
        },
        "action_status": "approved",
    }
    if decision:
        item["decision_id"] = decision
    return item


def test_a_standing_approval_is_remembered_by_its_decision_and_a_new_decision_is_not(tmp_path):
    acted = results.Acted(str(tmp_path / "acted"))
    acted.add(approved(decision="d1"))
    assert approved(decision="d1") in acted
    assert approved(decision="d2") not in acted


def test_without_a_decision_id_an_action_is_remembered_by_its_content(tmp_path):
    acted = results.Acted(str(tmp_path / "acted"))
    acted.add(approved(decision=None))
    assert approved(decision=None) in acted
    assert approved(to="r", decision=None) not in acted
    assert results.action_key(approved(decision=None)) == "p|swap|q"


def test_what_was_acted_on_survives_a_restart_with_when_it_may_be_forgotten(tmp_path):
    path = str(tmp_path / "acted")
    results.Acted(path).add(approved(decision="d1"))
    assert approved(decision="d1") in results.Acted(path)
    assert (tmp_path / "acted").read_text() == "d1\t2099-01-08T01:00:00+00:00\n"


def test_a_key_is_forgotten_a_week_after_its_action_is_over_and_a_live_one_is_kept(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "acted")
    acted = results.Acted(path)
    acted.add(approved(decision="old", year=2030))
    acted.add(approved(decision="live"))
    monkeypatch.setattr(results, "now", lambda: results.parse_time("2030-01-08T00:59:00Z"))
    assert approved(decision="old") in results.Acted(path)
    monkeypatch.setattr(results, "now", lambda: results.parse_time("2030-01-08T01:01:00Z"))
    restarted = results.Acted(path)
    assert approved(decision="old") not in restarted and approved(decision="live") in restarted
    assert (tmp_path / "acted").read_text() == "live\t2099-01-08T01:00:00+00:00\n"


def test_the_latest_of_the_pass_end_and_the_cancel_deadline_sets_when_a_key_is_forgotten():
    late_deadline = {
        **approved(),
        "action": {**approved()["action"], "deadline": "2099-02-01T00:00:00Z"},
    }
    assert results.acted_until(late_deadline) == "2099-02-08T00:00:00+00:00"
    assert results.acted_until({"entity": {"id": "p"}, "target_at": "soon"}) == ""


def test_expected_gb_needs_every_quantile_and_falls_back_to_the_value():
    full = {"p10": 2.0, "p50": 10, "p90": 11.0}
    assert results.expected_gb({"explain": {"expected_gb": {"value": full}}}) == full
    assert results.expected_gb({"explain": {"expected_gb": {"value": {"p10": 2.0}}}}) is None
    assert results.expected_gb({"explain": {"gb": {"value": full}}, "value": full}) == full
    assert results.expected_gb({"value": {**full, "p90": "11"}}) is None


def test_a_bridge_that_carries_out_approvals_each_run_counts_each_decision_once(monkeypatch):
    monkeypatch.setattr(results, "now", lambda: datetime(2098, 6, 1, tzinfo=timezone.utc))
    acted: dict[str, str] = {}
    assert results.first_acted([approved(decision="d1")], acted) == 1
    assert results.first_acted([approved(decision="d1")], acted) == 0
    assert results.first_acted([approved(decision="d2")], acted) == 1
    # Past its cancel deadline an approval is closed, never carried out.
    assert results.first_acted([approved(decision="d3", year=2030)], acted) == 0
    # A key whose action is long over is dropped.
    acted["old"] = "2030-01-01T00:00:00+00:00"
    results.first_acted([], acted)
    assert sorted(acted) == ["d1", "d2"]


def _feed(*connections: str | None) -> list[dict]:
    """A feed with one run per connection in turn; None is a run with nothing for this one."""
    entries = []
    for n, connection in enumerate(connections, 1):
        otlp = api.to_otlp("t", [], datetime.now(UTC), {"workflow": "w", "run_id": f"run_{n}"})
        entries.append(
            {"cursor": f"c{n:03d}#run_{n}", "otlp": otlp if connection else {"resourceLogs": []}}
        )
    return entries


class OlderApi(api.ResultsApi):
    """An API before contract 1.5.0: it ignores from=latest and serves a page."""

    def call(self, method, path, body=None):
        return super().call(method, path.replace("from=latest&", ""), body)


@pytest.mark.parametrize(
    "connections",
    [(), ("me",), ("me", None), ("me", "me", None, None), (None, None), ("me",) * 45],
)
def test_a_first_start_asks_the_api_once_where_to_read_from(connections):
    """The head lookup reads no run: one request, never a walk of the feed."""
    head = api.ResultsApi(_feed(*connections))
    assert results.start_cursor(head.call, "me") == api.start(head.entries)
    ((_, _, query),) = head.calls
    assert query["from"] == "latest" and query["consumer"] == "me"


def test_a_first_start_fails_plainly_against_an_api_that_ignores_from_latest():
    """An API before contract 1.5.0 serves the feed's first page instead: the bridge says so and
    reads nothing, rather than walking a week of the feed."""
    older = OlderApi(_feed("me", "me"))
    with pytest.raises(RuntimeError, match=r"from=latest.*contract 1\.5\.0 or later"):
        results.start_cursor(older.call, "me")
    assert len(older.calls) == 1


def test_the_stub_bounds_a_page_by_bytes_as_the_api_does():
    """A page stops at the byte cap, always with one entry, one over the cap alone and marked."""
    feed = _feed("me", "me", "me")
    size = len(json.dumps(api.ResultsApi(feed)._page(feed[0], 0), separators=(",", ":")))
    page = api.ResultsApi(feed, page_bytes=2 * size).call("GET", "/predictions/feed?after=")
    assert [e["cursor"] for e in page["entries"]] == [e["cursor"] for e in feed[:2]]
    (only,) = api.ResultsApi(feed, page_bytes=10).call("GET", "/predictions/feed")["entries"]
    assert only["oversized"] is True and only["cursor"] == feed[0]["cursor"]
