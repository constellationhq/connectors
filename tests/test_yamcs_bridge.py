"""The Yamcs bridge writes each run the way its workflow's feed output says. Entries are built with
the API's OTLP encoding, tokens table and default mappings (api.py), so the bridge is tested
against the contract Constellation publishes."""

import importlib.util
import io
import json
import os
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

import api
import pytest
from api import TOKENS, YAMCS_DEFAULTS, output, to_otlp
from constellation_space import results

SOURCE = Path(__file__).resolve().parents[1] / "clients/python/src/constellation_space/bridges/yamcs/bridge.py"
API = "https://api.test"


class FakeYamcs:
    """Answers the Yamcs and Constellation calls the bridge makes and records every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, Any]] = []
        self.sent: list[tuple[str, str, Any]] = []
        self.bands: list[dict] = []
        self.views: list[dict] = []
        # The timeline items Yamcs holds, by id: what the bridge wrote and what a person added.
        self.items: dict[str, dict] = {}
        # Yamcs answers `status` to calls whose path holds `refuse`, `times` times (None: always).
        self.refuse: str | None = None
        self.status = 403
        self.times: int | None = None
        # (part of a Constellation path, status, body): Constellation refuses those calls, with
        # api_headers.
        self.api_refusal: tuple[str, int, bytes] | None = None
        self.api_headers = Message()

    def __call__(self, method, url, body: Any = None, headers=None, raw=None):
        if url.startswith(API):
            self.sent.append((method, url.removeprefix(API), body))
            if self.api_refusal and self.api_refusal[0] in url:
                _, status, answer = self.api_refusal
                raise urllib.error.HTTPError(url, status, "", self.api_headers, io.BytesIO(answer))
            return None
        path = url.split("/api", 1)[1]
        if self.refuse and self.refuse in path and self.times != 0:
            self.times = None if self.times is None else self.times - 1
            answer = io.BytesIO(b'{"msg": "refused"}')
            raise urllib.error.HTTPError(url, self.status, "Refused", Message(), answer)
        if method == "GET":
            if path.endswith("/bands"):
                return {"bands": self.bands}
            if path.endswith("/views"):
                return {"views": self.views}
            if path.split("?")[0].endswith("/timeline/simulator/items"):
                return {"items": list(self.items.values())}
            if path.endswith("/lists"):
                return {"lists": []}
            if path == "/storage/buckets":
                return {"buckets": []}
            if path.endswith("index.json"):
                raise urllib.error.HTTPError(url, 404, "Not found", Message(), io.BytesIO())
            if path == "":
                return {"yamcsVersion": "5.14.0", "plugins": [{"name": "constellation-yamcs"}]}
            return {}
        self.writes.append((method, path, body if raw is None else raw))
        if "/timeline/simulator/items" in path:
            if method == "DELETE":
                self.items.pop(path.rsplit("/", 1)[1], None)
            elif method == "PUT":
                self.items[path.rsplit("/", 1)[1]] = {"id": path.rsplit("/", 1)[1], **body}
            else:
                self.items[body["id"]] = body
        if path.endswith("/bands"):
            self.bands.append({"id": f"b{len(self.bands)}", **body})
            return {"id": self.bands[-1]["id"]}
        return None

    def paths(self, prefix: str) -> list[str]:
        return [p for _, p, _ in self.writes if p.startswith(prefix)]

    def events(self) -> list[dict]:
        return [b for _, p, b in self.writes if p.endswith("/events")]

    def objects(self, name: str) -> list[dict]:
        return [json.loads(b) for _, p, b in self.writes if p.endswith(f"/objects/{name}")]


def load(monkeypatch) -> tuple[Any, "FakeYamcs"]:
    """A fresh bridge process: nothing in memory, only what ACTED_FILE kept."""
    spec = importlib.util.spec_from_file_location("yamcs_bridge", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = FakeYamcs()
    monkeypatch.setattr(module, "call", fake)
    return module, fake


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", API)
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_KEY", "k")
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    monkeypatch.setitem(os.environ, "ACTED_FILE", str(tmp_path / "acted.txt"))
    # The passes here are in November 2026: keep what was acted on from being forgotten as stale.
    monkeypatch.setattr(results, "now", lambda: datetime(2026, 11, 5, tzinfo=UTC))
    return load(monkeypatch)


Q = {"p10": 2.0, "p50": 10.0, "p90": 11.0}
PASS = {
    "entity": {
        "type": "pass",
        "id": "pass_1",
        "link_id": "link_1",
        "satellite_id": "sat-1",
        "ground_station_id": "gs",
        "antenna_id": "ant-1",
        "source": "orbit",
    },
    "target_at": "2026-11-05T10:00:00+00:00",
    "until": "2026-11-05T10:08:00+00:00",
    "value": Q,
    "explain": {
        "expected_gb": {"value": Q, "unit": "GB", "source": "expected_gb"},
        # The ledger keys each entry by the node that wrote it: the input node writes elevation.
        "passes": {"value": 40.0, "unit": "deg", "source": "orbit"},
    },
    "workflow_version": 3,
}
SWAP = {
    **PASS,
    "severity": "warning",
    "action": {
        "verb": "swap",
        "from": "pass_1",
        "to": "pass_2",
        "to_antenna_id": "ant-2",
        "to_at": "2026-11-05T11:00:00+00:00",
        "deadline": "2026-11-05T09:45:00+00:00",
        "mode": "suggest",
        "gain_gb_p10": 4.0,
    },
}
# The span of time a run is the whole answer for.
WINDOW = {"window_from": "2026-11-05T00:00:00+00:00", "window_to": "2026-11-06T00:00:00+00:00"}
# A pass on another connection's output in the same run; this bridge never writes it.
ELSEWHERE = {**PASS, "entity": {**PASS["entity"], "id": "pass_x", "satellite_id": "sat-x"}}


def resource_logs(
    items: list[dict],
    template: str = "pass-yield",
    connection: str = "yamcs-bridge",
    mapping: dict | None = None,
    widget: dict | None = None,
) -> list[dict]:
    default_mapping, default_widget = YAMCS_DEFAULTS[template]
    feed_output = output(connection, mapping or default_mapping, widget or default_widget)
    run = {"workflow": "w", "run_id": "run_1", **WINDOW}
    when = datetime(2026, 11, 5, tzinfo=UTC)
    return to_otlp("t", items, when, run, feed_output)["resourceLogs"]


def entry(items, template: str = "pass-yield", **output) -> dict:
    """A feed entry as the API publishes one: a resource per feed output, here this bridge's and
    one for another connection."""
    logs = resource_logs(items, template, **output) + resource_logs(
        [ELSEWHERE], connection="ops-yamcs"
    )
    return {"cursor": "c#run_1", "otlp": {"resourceLogs": logs}}


def feed_api(feed: list[dict], sent: list | None = None, oldest: str | None = None):
    """The feed routes over `feed` as the API answers them, as the bridge's constellation():
    `after` with `limit`, one run by `cursor`, or from=latest. Each GET's query goes in `feed_api.queries`,
    every other call in `sent`."""

    def constellation(method, path, body=None):
        if method != "GET":
            if sent is not None:
                sent.append((method, path, body))
            return None
        query = dict(urllib.parse.parse_qsl(path.split("?", 1)[1], keep_blank_values=True))
        feed_api.queries.append(query)
        if "cursor" in query:
            return next(e for e in feed if e["cursor"] == query["cursor"])
        if query.get("from") == "latest":
            return {"entries": [], "next": api.start(feed), "oldest": oldest}
        entries = [e for e in feed if e["cursor"] > query["after"]][: int(query["limit"])]
        nxt = entries[-1]["cursor"] if entries else query["after"]
        return {"entries": entries, "next": nxt, "oldest": oldest}

    feed_api.queries = []
    return constellation


def test_a_pass_yield_run_writes_events_timeline_parameters_and_the_widget(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    (event,) = yamcs.events()
    assert event["message"] == (
        "Forecast: predicted pass sat-1 over ant-1 at 2026-11-05 10:00Z, "
        "10.0 GB expected (p10 2.0, p90 11.0)"
    )
    assert (event["severity"], event["source"], event["type"]) == (
        "INFO",
        "Constellation forecast",
        "w",
    )
    (put,) = [b for m, p, b in yamcs.writes if m == "PUT" and "/timeline/" in p and "/items/" in p]
    assert put["name"] == "Forecast · sat-1 @ ant-1: 10.0 GB expected"
    assert "band-forecast-predicted-passes" in put["tags"]
    assert put["properties"]["expected_gb_p50"] == "10.0"
    assert put["properties"]["workflow_version"] == "3" and put["properties"]["run_id"] == "run_1"
    assert yamcs.paths("/processors/")
    (published,) = yamcs.objects("workflow-w.json")
    assert published["kind"] == "pass-yield" and published["items"] == [PASS]
    assert module.written.keys() == {"events", "timeline_items", "parameters", "widget_updates"}


def test_resources_for_another_connection_are_ignored(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    assert all("sat-x" not in json.dumps(b, default=str) for _, _, b in yamcs.writes)
    # A run with nothing for this connection writes nothing at all.
    other = {"cursor": "c#run_2", "otlp": {"resourceLogs": resource_logs([PASS], connection="x")}}
    yamcs.writes.clear()
    module.deliver(other, {})
    assert yamcs.writes == [] and module.written == {}


def test_two_outputs_for_this_connection_are_each_applied_by_their_own_mapping(bridge):
    module, yamcs = bridge
    logs = resource_logs([PASS]) + resource_logs([SWAP], "rain-fade-swap")
    module.deliver({"cursor": "c#run_1", "otlp": {"resourceLogs": logs}}, {})
    assert [e["severity"] for e in yamcs.events()] == ["INFO", "WARNING"]


def test_the_bridge_makes_its_band_and_view_once_and_never_edits_others(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    module.deliver(entry([PASS]), {})
    assert [b["name"] for b in yamcs.bands] == ["Forecast · predicted passes", "UTC"]
    (view,) = [b for _, p, b in yamcs.writes if p.endswith("/views")]
    assert view["name"] == "Constellation forecast"


def test_with_create_views_off_the_bridge_makes_no_band(bridge, monkeypatch):
    module, yamcs = bridge
    monkeypatch.setattr(module, "CREATE_VIEWS", False)
    module.deliver(entry([PASS]), {})
    assert yamcs.bands == [] and yamcs.paths("/timeline/simulator/items")


def test_a_rain_fade_swap_is_a_warning_with_the_backup_and_no_timeline(bridge):
    module, yamcs = bridge
    module.deliver(entry([SWAP], "rain-fade-swap"), {})
    (event,) = yamcs.events()
    assert event["severity"] == "WARNING"
    assert event["message"] == (
        "Recommendation (forecast): rain fade risk for sat-1 over ant-1 at 2026-11-05 10:00Z, "
        "keeps 2.0 of 10.0 GB at p10; backup ant-2; cancel by 2026-11-05 09:45Z"
    )
    assert not yamcs.paths("/timeline/") and not yamcs.paths("/processors/")
    (published,) = yamcs.objects("workflow-w.json")
    assert published["kind"] == "rain-fade-swap"


def test_a_swap_without_a_backup_arrives_without_to_and_still_warns(bridge):
    module, yamcs = bridge
    alone = {**SWAP, "action": {"verb": "swap", "deadline": SWAP["action"]["deadline"]}}
    module.deliver(entry([alone], "rain-fade-swap"), {})
    (event,) = yamcs.events()
    assert event["severity"] == "WARNING" and "backup ; cancel by" in event["message"]


def test_a_modcod_plan_lists_each_change_on_one_timeline_item(bridge):
    module, yamcs = bridge
    series = [
        {"t": "2026-11-05T10:00:00+00:00", "value": {"modcod": "QPSK 1/2"}},
        {"t": "2026-11-05T10:03:00+00:00", "value": {"modcod": "8PSK 3/5"}},
    ]
    module.deliver(entry([{**PASS, "series": series}], "modcod-plan"), {})
    (put,) = [b for m, p, b in yamcs.writes if m == "PUT" and "/items/" in p]
    assert put["properties"]["plan"] == "10:00 QPSK 1/2 → 10:03 8PSK 3/5"
    assert "band-forecast-modcod-plan" in put["tags"]
    assert not yamcs.events() and not yamcs.paths("/processors/")


def test_an_edited_mapping_is_followed_exactly(bridge):
    module, yamcs = bridge
    mapping = {
        "system": "yamcs",
        "events": {
            "on": True,
            "message": "{satellite}: {expected_gb.p10} at worst",
            "source": "Ops forecast",
            "severity": "INFO",
            "warn_if": {"field": "expected_gb.p10", "op": "<", "value": 5},
        },
        "timeline": {
            "on": False,
            "band": "Forecast · predicted passes",
            "name": "x",
            "tags": ["constellation", "forecast"],
        },
        "parameters": {"on": False},
    }
    module.deliver(entry([PASS], mapping=mapping, widget={"on": False, "blocks": ["list"]}), {})
    assert yamcs.events() == [
        {
            "message": "sat-1: 2.0 at worst",
            "type": "w",
            "severity": "WARNING",
            "source": "Ops forecast",
            "time": "2026-11-05T00:00:00Z",
        }
    ]
    assert [p for _, p, _ in yamcs.writes] == ["/archive/simulator/events"]


def test_a_newer_mapping_than_the_bridge_knows_is_refused_not_guessed(bridge, monkeypatch, capsys):
    module, yamcs = bridge
    monkeypatch.setattr(module, "MAPPING_VERSIONS", [1])
    module.deliver(entry([PASS]), {})
    assert yamcs.writes == [] and module.written == {"refused": 1}
    assert "needs a newer bridge" in capsys.readouterr().out


def test_an_output_for_another_system_is_refused_and_the_rest_of_the_run_applied(bridge, capsys):
    module, yamcs = bridge
    lattice = output("yamcs-bridge", {"system": "lattice"})
    run = {"workflow": "w", "run_id": "run_1"}
    logs = to_otlp("t", [PASS], datetime(2026, 11, 5, tzinfo=UTC), run, lattice)
    both = {"cursor": "c#run_1", "otlp": {"resourceLogs": logs["resourceLogs"]}}
    both["otlp"]["resourceLogs"] += entry([PASS])["otlp"]["resourceLogs"]
    module.deliver(both, {})
    assert module.written["refused"] == 1 and module.written["events"] == 1
    assert "lattice" in capsys.readouterr().out


def test_a_refused_run_is_reported_and_the_feed_moves_past_it(bridge, monkeypatch):
    """One bad output never blocks every later run: its cursor is applied and reported."""
    module, yamcs = bridge
    run_1 = to_otlp(
        "t",
        [PASS],
        datetime(2026, 11, 5, tzinfo=UTC),
        {"workflow": "refused", "run_id": "run_1"},
        output("yamcs-bridge", {"system": "lattice"}),
    )
    feed = [
        {"cursor": "20261105T000001.000000#run_1", "otlp": run_1},
        {**entry([PASS]), "cursor": "20261105T000002.000000#run_2"},
    ]

    sleeps = iter(range(3))
    monkeypatch.setattr(module, "constellation", feed_api(feed, yamcs.sent))
    monkeypatch.setattr(module, "saved_cursor", lambda: "20261105T000000.000000#run_0")
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    reports = {b["run_id"]: b["applied"] for _, p, b in yamcs.sent if p.endswith("/deliveries")}
    assert reports["run_1"] == {"refused": 1} and reports["run_2"]["events"] == 1


def test_a_cursor_older_than_the_feed_holds_says_runs_expired_and_reads_on(
    bridge, monkeypatch, capsys
):
    module, yamcs = bridge
    oldest = "20261105T000005.000000#run_5"
    feed = [{**entry([PASS]), "cursor": oldest}]

    sleeps = iter(range(1))
    monkeypatch.setattr(module, "constellation", feed_api(feed, oldest=oldest))
    monkeypatch.setattr(module, "saved_cursor", lambda: "20261105T000001.000000#run_1")
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    err = capsys.readouterr().err
    assert "expired" in err and "run_1" in err and oldest in err
    assert yamcs.events()


def test_every_token_the_api_offers_resolves_from_some_item(bridge):
    module, _ = bridge
    plan = {
        **PASS,
        "series": [{"t": "2026-11-05T10:00:00+00:00", "value": {"modcod": "QPSK 1/2"}}],
        "value": {**Q, "modcod_changes": 1},
    }
    antenna = {
        **PASS,
        "entity": {"type": "antenna", "id": "ant-1", "ground_station_id": "gs"},
        "value": {"passes": 9, "sellable_min": 72.0, **Q},
        "explain": {"sum_by": {"value": {"passes": 9, "sellable_min": 72.0, **Q}, "unit": "GB"}},
    }
    ledger = {"possible_gb": 9.0, "delivered_gb": 8.0, "status": "delivered"}
    audit = {**PASS, "value": ledger, "explain": {"ledger": {"value": ledger}}}
    risk = {**PASS, "value": {"p10": 1.0, "p50": 2.0, "p90": 3.0, "lock_loss_risk": 0.2}}
    resolved_ = [module.fields(i, TOKENS) for i in (PASS, SWAP, plan, antenna, audit, risk)]
    assert {t for t in TOKENS if all(f[t] is None for f in resolved_)} == set()
    assert module.fields(antenna, TOKENS)["day"] == "2026-11-05"
    assert (
        module.render("{satellite} {nope} {expected_gb.p50}", module.fields(PASS, TOKENS))
        == "sat-1  10.0"
    )


def test_a_refusal_from_yamcs_names_the_missing_grant(bridge):
    module, yamcs = bridge
    yamcs.refuse = "/archive/"
    with pytest.raises(PermissionError, match="may not do that"):
        module.deliver(entry([PASS]), {})


def test_grants_read_from_the_bridge_account(bridge):
    module, _ = bridge
    assert all(module.grants({"superuser": True}).values())
    assert module.grants(
        {
            "systemPrivileges": ["WriteEvents"],
            "objectPrivileges": [{"type": "ManageBucket", "objects": ["constellation"]}],
        }
    ) == {"events": True, "timeline": False, "parameters": False, "buckets": True}


def test_check_in_names_the_mapping_versions_this_bridge_reads(bridge):
    module, yamcs = bridge
    module.check_in()
    ((method, path, body),) = yamcs.sent
    assert (method, path) == ("POST", "/predictions/feed/checkin")
    assert body["mapping_versions"] == [2] and body["system"] == "yamcs"
    assert body["widget"]["installed"] is True


def test_a_rerun_writes_events_only_for_new_or_changed_passes(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    module.deliver(entry([PASS]), {})
    assert len(yamcs.events()) == 1
    assert module.written.get("unchanged") == 1
    lower = {"p10": 1.0, "p50": 7.0, "p90": 9.0}
    changed = {
        **PASS,
        "value": lower,
        "explain": {**PASS["explain"], "expected_gb": {"value": lower}},
    }
    other = {**PASS, "entity": {**PASS["entity"], "id": "pass_2"}}
    module.deliver(entry([changed, other]), {})
    assert len(yamcs.events()) == 3
    timeline_ids = {
        p.rsplit("/", 1)[1] for m, p, _ in yamcs.writes if m == "PUT" and "/items/" in p
    }
    assert len(timeline_ids) == 2


def capacity_day(day: str, p50: float) -> dict:
    """An antenna-capacity item: one antenna, one UTC day."""
    gb = {"passes": 2, "sellable_min": 8.0, "p10": p50 - 4, "p50": p50, "p90": p50 + 1}
    return {
        "entity": {"type": "antenna", "id": "ant-1", "ground_station_id": "gs"},
        "target_at": f"{day}T00:00:00+00:00",
        "until": f"{day}T23:59:59+00:00",
        "value": gb,
        "explain": {"sum_by": {"value": gb, "unit": "GB", "source": "sum_by"}},
        "workflow_version": 3,
    }


def test_each_day_of_an_antennas_capacity_is_its_own_event_and_timeline_item(bridge):
    module, yamcs = bridge
    mapping, _ = YAMCS_DEFAULTS["antenna-capacity"]
    mapping = {**mapping, "timeline": {**mapping["timeline"], "on": True}}
    days = [capacity_day("2026-11-05", 14.0), capacity_day("2026-11-06", 9.0)]
    module.deliver(entry(days, "antenna-capacity", mapping=mapping), {})
    module.deliver(entry(days, "antenna-capacity", mapping=mapping), {})
    assert [e["message"][:40] for e in yamcs.events()] == [
        "Forecast capacity ant-1 on 2026-11-05: 2",
        "Forecast capacity ant-1 on 2026-11-06: 2",
    ]
    assert len({p for m, p, _ in yamcs.writes if m == "PUT" and "/items/" in p}) == 2


def test_a_small_share_is_never_rounded_to_zero(bridge):
    module, _ = bridge
    values = {"lock_loss_risk": 0.033, "gb": 10.04, "none": 0.0, "half": 0.5}
    assert module.render("{lock_loss_risk} {gb} {none} {half}", values) == "0.033 10.0 0.0 0.5"


def test_the_parameters_are_the_next_pass_of_the_newest_run(bridge):
    module, _ = bridge
    # A pass on a radio with no RF data: the API leaves its value out.
    unpriced = {
        **{k: v for k, v in PASS.items() if k != "value"},
        "entity": {**PASS["entity"], "id": "pass_0"},
        "explain": {},
    }
    passes = module.priced_passes([PASS, SWAP, unpriced], TOKENS)
    assert list(passes) == ["pass_1"]
    got = module.summary(list(passes.values()), datetime(2026, 11, 5, 9, tzinfo=UTC))
    assert got["passes_next_24h"] == 1
    assert got["next_pass_expected_gb_p50"] == 10.0
    assert got["next_pass_max_elevation"] == 40.0
    assert got["next_pass_aos"] == PASS["target_at"]


def test_the_delivery_report_names_the_run_and_what_was_written(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    module.report("20261105T060003.479811#run_1")
    ((_, path, body),) = yamcs.sent
    assert path == "/predictions/feed/deliveries"
    assert (body["consumer"], body["run_id"], body["applied"]["events"]) == (
        "yamcs-bridge",
        "run_1",
        1,
    )


def test_items_in_a_shape_newer_than_the_bridge_reads_are_skipped_not_misread(
    bridge, monkeypatch, capsys
):
    module, yamcs = bridge
    monkeypatch.setattr(api, "SCHEMA_VERSION", "constellation.item/v3")
    module.deliver(entry([PASS]), {})
    assert yamcs.writes == [] and module.written == {}
    assert "constellation.item/v3" in capsys.readouterr().out


PLAN = json.dumps(
    {"detail": {"error": "plan_required", "plan": "enterprise", "feature": "output:yamcs"}}
).encode()


LOCKOUT = json.dumps({"error": "auth_lockout", "retry_after_seconds": 900}).encode()
TOO_MANY = json.dumps({"detail": {"error": "too_many_consumers", "max": 50}}).encode()
UPGRADE = json.dumps({"detail": {"error": "upgrade_required", "min_plan": "team"}}).encode()


@pytest.mark.parametrize("refused", ["/predictions/feed/checkin", "/predictions/feed?"])
@pytest.mark.parametrize(
    "status, body, says, wait",
    [
        (403, PLAN, "plan does not include output:yamcs", 600),
        (401, PLAN, "401", 600),
        (429, LOCKOUT, "auth_lockout", 900),
        (429, TOO_MANY, "too_many_consumers", 600),
        (402, UPGRADE, "HTTP 402 upgrade_required", 600),
    ],
)
def test_a_refusal_is_said_once_and_asked_again_every_10_minutes_never_exiting(
    bridge, monkeypatch, capsys, refused, status, body, says, wait
):
    """A refused key, plan or account does not clear by asking again soon, and a restart loop
    hitting a refused key can lock out the address: the bridge says why and the fix in one line,
    then asks again only every 10 minutes (longer while locked out)."""
    module, yamcs = bridge
    yamcs.api_refusal = (refused, status, body)
    yamcs.api_headers["Retry-After"] = "900"
    monkeypatch.setattr(module, "saved_cursor", lambda: "20261105T000000.000000#run_0")
    slept = stop_after_sleeps(monkeypatch, module, 2)
    with pytest.raises(SystemExit):
        module.main()
    assert slept == [wait, wait]
    assert len([p for _, p, _ in yamcs.sent if p.startswith(refused)]) == 3
    (line,) = capsys.readouterr().err.splitlines()
    assert says in line and "Traceback" not in line


def test_a_malformed_item_is_skipped_logged_and_counted_and_the_rest_applied(bridge, capsys):
    module, yamcs = bridge
    no_time = {**PASS, "entity": {**PASS["entity"], "id": "pass_2"}, "target_at": "soon"}
    no_id = {**PASS, "entity": {**PASS["entity"], "id": 7}}
    module.deliver(entry([no_time, PASS, no_id]), {})
    assert module.written["malformed"] == 2 and module.written["events"] == 1
    (published,) = yamcs.objects("workflow-w.json")
    assert published["items"] == [PASS]
    assert capsys.readouterr().out.count("it is malformed") == 2


def test_a_yamcs_4xx_on_an_item_is_logged_and_skipped_not_retried(bridge, monkeypatch, capsys):
    module, yamcs = bridge
    yamcs.refuse, yamcs.status = "/archive/", 400
    monkeypatch.setattr(module.time, "sleep", lambda s: pytest.fail("retried a 4xx"))
    module.deliver(entry([PASS]), {})
    assert module.written["rejected"] == 1 and yamcs.objects("workflow-w.json")
    assert "Yamcs refused it (HTTP 400" in capsys.readouterr().out


def test_a_yamcs_5xx_is_retried_with_backoff_then_the_run_is_delivered_again(bridge, monkeypatch):
    """Yamcs failing: each call is tried again 1, 2 and 4 s later; still failing, the run is not
    acked, and the next poll delivers it again."""
    module, yamcs = bridge
    yamcs.refuse, yamcs.status, yamcs.times = "/archive/", 503, 4
    feed = [{**entry([PASS]), "cursor": "20261105T000001.000000#run_1"}]
    sent, slept = [], []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 5:
            raise StopIteration

    monkeypatch.setattr(module, "constellation", feed_api(feed, sent))
    monkeypatch.setattr(module, "saved_cursor", lambda: "20261105T000000.000000#run_0")
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    assert slept[:4] == [1, 2, 4, module.POLL_S]
    assert [b["run_id"] for _, p, b in sent if p.endswith("/deliveries")] == ["run_1"]
    assert len(yamcs.events()) == 1


def test_a_resource_tagged_for_another_system_is_refused_whatever_its_output_says(bridge, capsys):
    module, yamcs = bridge
    module.deliver({**entry([PASS]), "otlp": api.tagged(entry([PASS])["otlp"], "lattice")}, {})
    assert yamcs.writes == [] and module.written == {"refused": 1}
    assert "it is a lattice output, not a Yamcs one" in capsys.readouterr().out


APPROVED = {**SWAP, "action": {**SWAP["action"], "mode": "approve"}, "action_status": "approved"}
HELD = {**{k: v for k, v in SWAP.items() if k != "action"}, "action_status": "pending"}
REJECTED = {**HELD, "action_status": "rejected"}


def swap_mapping(**changes) -> dict:
    """The rain-fade-swap default mapping with `events`, `timeline` or `parameters` changed."""
    mapping, _ = YAMCS_DEFAULTS["rain-fade-swap"]
    return {**mapping, **{k: {**mapping[k], **v} for k, v in changes.items()}}


def swap_run(items: list[dict], mapping: dict | None = None, run_id: str = "run_1") -> dict:
    """An approve-policy rain-fade-swap run as the API publishes it, its template named."""
    default_mapping, default_widget = YAMCS_DEFAULTS["rain-fade-swap"]
    feed_output = output("yamcs-bridge", mapping or default_mapping, default_widget)
    run = {"workflow": "w", "run_id": run_id, "template": "rain-fade-swap", **WINDOW}
    logs = to_otlp("t", items, datetime(2026, 11, 5, tzinfo=UTC), run, feed_output)
    return {"cursor": f"c#{run_id}", "otlp": logs}


@pytest.mark.parametrize(
    ("configured", "sent"), [("INFO", "WARNING"), ("WARNING", "WARNING"), ("CRITICAL", "CRITICAL")]
)
def test_an_approved_swap_is_an_instruction_at_warning_or_above(bridge, configured, sent):
    module, yamcs = bridge
    module.deliver(swap_run([APPROVED], swap_mapping(events={"severity": configured})), {})
    (event,) = yamcs.events()
    assert event["severity"] == sent
    assert event["message"].startswith("Approved in Constellation: Recommendation (forecast)")
    assert "backup ant-2; cancel by 2026-11-05 09:45Z" in event["message"]


def test_a_held_swap_writes_no_event_and_its_run_still_reads_as_a_swap_run(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([HELD]), {})
    assert yamcs.events() == []
    (published,) = yamcs.objects("workflow-w.json")
    assert published["kind"] == "rain-fade-swap"


def test_a_held_swap_on_the_timeline_is_tagged_awaiting_approval(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([HELD], swap_mapping(timeline={"on": True})), {})
    (put,) = [b for m, p, b in yamcs.writes if m == "PUT" and "/items/" in p]
    assert "awaiting-approval" in put["tags"] and put["properties"]["action_status"] == "pending"


def test_an_approved_swap_on_the_timeline_names_the_backup_and_deadline(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([APPROVED], swap_mapping(timeline={"on": True})), {})
    (put,) = [b for m, p, b in yamcs.writes if m == "PUT" and "/items/" in p]
    assert "approved" in put["tags"]
    assert put["properties"]["action_status"] == "approved"
    assert put["properties"]["backup_antenna"] == "ant-2"
    assert put["properties"]["deadline"] == SWAP["action"]["deadline"]


def test_approving_a_held_swap_writes_one_event_then_and_none_before(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([HELD]), {})
    module.deliver(swap_run([APPROVED]), {})
    # Reported as acted once, so the platform shows the approval carried out.
    assert module.written.get("acted") == 1
    module.deliver(swap_run([APPROVED]), {})
    assert "acted" not in module.written
    (event,) = yamcs.events()
    assert event["message"].startswith("Approved in Constellation: ")


def test_an_approved_swap_a_later_run_sends_again_writes_no_second_event_even_reworded(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([APPROVED]), {})
    lower = {"p10": 1.0, "p50": 7.0, "p90": 9.0}
    reworded = {**APPROVED, "value": lower, "explain": {**APPROVED["explain"], "expected_gb": {"value": lower}}}
    module.deliver(swap_run([reworded], run_id="run_2"), {})
    (event,) = yamcs.events()
    assert event["message"].startswith("Approved in Constellation: ")


def test_an_approved_swap_announced_before_a_restart_is_not_announced_again(bridge, monkeypatch):
    module, yamcs = bridge
    module.deliver(swap_run([APPROVED]), {})
    assert len(yamcs.events()) == 1
    restarted, yamcs = load(monkeypatch)
    restarted.deliver(swap_run([APPROVED], run_id="run_2"), {})
    assert yamcs.events() == []
    # Another backup for the same pass is another action, announced in its turn.
    other = {**APPROVED, "action": {**APPROVED["action"], "to": "pass_3"}}
    restarted.deliver(swap_run([other], run_id="run_3"), {})
    assert len(yamcs.events()) == 1


def test_a_standing_approval_is_announced_once_and_a_new_approval_of_it_again(bridge, monkeypatch):
    module, yamcs = bridge
    module.deliver(swap_run([{**APPROVED, "decision_id": "d1"}]), {})
    restarted, yamcs = load(monkeypatch)
    restarted.deliver(swap_run([{**APPROVED, "decision_id": "d1"}], run_id="run_2"), {})
    assert yamcs.events() == []
    restarted.deliver(swap_run([{**APPROVED, "decision_id": "d2"}], run_id="run_3"), {})
    (event,) = yamcs.events()
    assert event["message"].startswith("Approved in Constellation: ")


def test_an_approved_swap_past_its_cancel_deadline_is_never_announced(bridge, monkeypatch):
    module, yamcs = bridge
    monkeypatch.setattr(results, "now", lambda: datetime(2026, 11, 6, tzinfo=UTC))
    module.deliver(swap_run([APPROVED]), {})
    module.deliver(swap_run([APPROVED], run_id="run_2"), {})
    assert yamcs.events() == []


def test_a_rejected_swap_writes_no_event_and_retags_its_held_timeline_item(bridge):
    module, yamcs = bridge
    mapping = swap_mapping(timeline={"on": True})
    module.deliver(swap_run([HELD], mapping), {})
    module.deliver(swap_run([REJECTED], mapping, run_id="run_2"), {})
    assert yamcs.events() == []
    held, rejected = [(p, b) for m, p, b in yamcs.writes if m == "PUT" and "/items/" in p]
    assert held[0] == rejected[0]
    assert "rejected" in rejected[1]["tags"] and "awaiting-approval" not in rejected[1]["tags"]


def test_a_held_action_sent_with_its_body_writes_no_event(bridge):
    module, yamcs = bridge
    module.deliver(swap_run([{**SWAP, "action_status": "pending"}]), {})
    assert yamcs.events() == []


def test_held_rejected_and_approved_items_are_never_the_forecast_passes(bridge):
    module, yamcs = bridge
    assert module.priced_passes([HELD, REJECTED, APPROVED], TOKENS) == {}
    # A swap mapping with parameters on: a held-only run leaves the pass-yield run's values.
    module.deliver(entry([PASS]), {})
    before = len(yamcs.paths("/processors/"))
    module.deliver(swap_run([HELD], swap_mapping(parameters={"on": True})), {})
    assert len(yamcs.paths("/processors/")) == before


def test_a_pass_the_next_run_no_longer_carries_is_deleted_from_the_timeline(bridge):
    module, yamcs = bridge
    other = {**PASS, "entity": {**PASS["entity"], "id": "pass_2"}}
    # A person's own item in the same band: never the bridge's to delete.
    yamcs.items["theirs"] = {"id": "theirs", "tags": ["band-forecast-predicted-passes"]}
    module.deliver(entry([PASS, other]), {})
    assert len(yamcs.items) == 3
    module.deliver(entry([PASS]), {})
    deletes = [p for m, p, _ in yamcs.writes if m == "DELETE"]
    assert len(deletes) == 1 and deletes[0].rsplit("/", 1)[1] not in yamcs.items
    assert "theirs" in yamcs.items and len(yamcs.items) == 2
    assert module.written.get("timeline_deleted") == 1


def test_a_run_deletes_only_its_own_window_and_never_a_pass_already_flown(bridge, monkeypatch):
    """A pass-yield run covers the next window only: passes before it or after it, and any that
    already started, are not withdrawn by it."""
    module, yamcs = bridge
    ids = ("flown", "tomorrow", "later_today")
    starts = ("2026-11-04T23:00:00+00:00", "2026-11-06T02:00:00+00:00", "2026-11-05T20:00:00+00:00")
    for id_, start in zip(ids, starts):
        yamcs.items[id_] = {
            "id": id_,
            "start": start,
            "tags": ["band-forecast-predicted-passes", module.MADE_BY_BRIDGE],
            "properties": {"workflow": "w"},
        }
    module.deliver(entry([PASS]), {})
    assert {"flown", "tomorrow"} <= set(yamcs.items) and "later_today" not in yamcs.items
    # Later the same day the morning's pass has started: no run withdraws it any more.
    monkeypatch.setattr(results, "now", lambda: datetime(2026, 11, 5, 10, 5, tzinfo=UTC))
    module.deliver(entry([]), {})
    assert len([i for i in yamcs.items.values() if i.get("name")]) == 1
    assert module.written.get("timeline_deleted") is None


def test_a_run_without_a_window_deletes_nothing(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    run = {"workflow": "w", "run_id": "run_2"}
    feed_output = output("yamcs-bridge", *YAMCS_DEFAULTS["pass-yield"])
    logs = to_otlp("t", [], datetime(2026, 11, 5, tzinfo=UTC), run, feed_output)
    module.deliver({"cursor": "c#run_2", "otlp": logs}, {})
    assert len(yamcs.items) == 1


def test_a_timeline_listing_that_never_ends_is_read_once_and_only_in_the_window(
    bridge, monkeypatch
):
    """Yamcs 5.14 with 500 or more items answers the same page with a new token forever."""
    module, yamcs = bridge
    stale = {
        "id": "stale",
        "start": "2026-11-05T20:00:00+00:00",
        "tags": ["band-forecast-predicted-passes", module.MADE_BY_BRIDGE],
        "properties": {"workflow": "w"},
    }
    listed = []

    def endless(method, path, *args, **kwargs):
        if method == "GET" and "/timeline/simulator/items?" in path:
            listed.append(dict(urllib.parse.parse_qsl(path.split("?", 1)[1])))
            assert len(listed) < 10, "paged on forever"
            return {"items": [stale], "continuationToken": f"t{len(listed)}"}
        return yamcs(method, f"{module.YAMCS}/api{path}", *args, **kwargs)

    monkeypatch.setattr(module, "yamcs", endless)
    module.deliver(entry([]), {})
    assert len(listed) == 2 and module.written.get("timeline_deleted") == 1
    assert (listed[0]["start"], listed[0]["stop"]) == (
        "2026-11-05T00:00:00+00:00",
        WINDOW["window_to"],
    )


def test_a_run_never_deletes_another_workflows_timeline_items(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    kept = set(yamcs.items)
    run = {"workflow": "other", "run_id": "run_2"}
    feed_output = output("yamcs-bridge", *YAMCS_DEFAULTS["pass-yield"])
    logs = to_otlp("t", [], datetime(2026, 11, 5, tzinfo=UTC), run, feed_output)
    module.deliver({"cursor": "c#run_2", "otlp": logs}, {})
    assert set(yamcs.items) == kept


def test_an_action_no_longer_approved_loses_its_approved_tag_on_the_timeline(bridge):
    module, yamcs = bridge
    mapping = swap_mapping(timeline={"on": True})
    module.deliver(swap_run([APPROVED], mapping), {})
    ((_, item),) = yamcs.items.items()
    assert "approved" in item["tags"]
    module.deliver(swap_run([SWAP], mapping, run_id="run_2"), {})
    ((_, item),) = yamcs.items.items()
    assert "approved" not in item["tags"]
    closed = {**APPROVED, "action": {**APPROVED["action"], "deadline": "2020-01-01T00:00:00+00:00"}}
    module.deliver(swap_run([closed], mapping, run_id="run_3"), {})
    ((_, item),) = yamcs.items.items()
    assert "approved" not in item["tags"]
    assert len(yamcs.events()) == 2


def test_events_carry_the_runs_own_time_not_now(bridge):
    module, yamcs = bridge
    module.deliver(entry([PASS]), {})
    assert yamcs.events()[0]["time"] == "2026-11-05T00:00:00Z"


def runs_feed(monkeypatch, module, runs):
    """The bridge's feed as the API serves it: one run per page, after the cursor given."""
    feed = [{**entry([PASS]), "cursor": f"c{n}#{rid}"} for n, (rid, wf) in enumerate(runs)]
    for e, (_, wf) in zip(feed, runs):
        for logs in e["otlp"]["resourceLogs"]:
            for kv in logs["resource"]["attributes"]:
                if kv["key"] == "constellation.workflow":
                    kv["value"] = {"stringValue": wf}

    monkeypatch.setattr(module, "constellation", feed_api(feed))
    return feed


def test_a_catch_up_applies_only_the_newest_run_of_each_workflow(bridge, monkeypatch):
    module, _ = bridge
    runs_feed(monkeypatch, module, [("r1", "a"), ("r2", "a"), ("r3", "b"), ("r4", "a")])
    got = [(c, e and e["cursor"]) for c, e in module.catch_up("")]
    assert got == [("c0#r1", None), ("c1#r2", None), ("c2#r3", "c2#r3"), ("c3#r4", "c3#r4")]


def test_a_live_run_is_applied_as_it_arrives(bridge, monkeypatch):
    module, _ = bridge
    runs_feed(monkeypatch, module, [("r1", "a")])
    assert [(c, e is not None) for c, e in module.catch_up("")] == [("c0#r1", True)]


def test_a_first_start_applies_its_own_newest_run_when_a_newer_one_is_for_another_connection(
    bridge, monkeypatch
):
    module, yamcs = bridge
    feed = [
        {**entry([PASS]), "cursor": "c1#run_1"},
        {"cursor": "c2#run_2", "otlp": {"resourceLogs": []}},
    ]

    monkeypatch.setattr(module, "constellation", feed_api(feed))
    monkeypatch.setattr(module.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        module.main()
    assert len(yamcs.events()) == 1


def test_the_acted_file_lives_in_state_dir(monkeypatch, tmp_path):
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", API)
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_KEY", "k")
    monkeypatch.delitem(os.environ, "ACTED_FILE", raising=False)
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    module, _ = load(monkeypatch)
    assert module.ACTED_FILE == f"{tmp_path}/yamcs-bridge-acted.txt"


def failing(status: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(API, status, "", headers, io.BytesIO(b"{}"))


def workflow_runs(*workflows: str) -> list[dict]:
    """One run per workflow named, in order, each a pass-yield run."""
    feed = []
    for n, wf in enumerate(workflows, 1):
        run = {**entry([PASS]), "cursor": f"c{n}#run_{n}"}
        for logs in run["otlp"]["resourceLogs"]:
            for kv in logs["resource"]["attributes"]:
                if kv["key"] in ("constellation.workflow", "constellation.run_id"):
                    kv["value"] = {"stringValue": wf if "workflow" in kv["key"] else f"run_{n}"}
        feed.append(run)
    return feed


def test_a_catch_up_saves_each_run_as_it_goes_and_waits_out_a_429(bridge, monkeypatch):
    """Rate limited halfway through a backlog: the runs applied stay applied, the bridge waits
    as long as the API asks, then reads on from the last run it applied."""
    module, yamcs = bridge
    sent: list = []
    serve = feed_api(workflow_runs("a", "b", "c"), sent)
    errors = [failing(429, "7")]

    def constellation(method, path, body=None):
        if errors and "cursor=c3" in path:
            raise errors.pop()
        return serve(method, path, body)

    slept = stop_after_sleeps(monkeypatch, module, 1)
    monkeypatch.setattr(module, "constellation", constellation)
    monkeypatch.setattr(module, "saved_cursor", lambda: "c0#run_0")
    with pytest.raises(SystemExit):
        module.main()
    assert slept == [7]
    assert [b["run_id"] for _, p, b in sent if p.endswith("/deliveries")] == [
        "run_1",
        "run_2",
        "run_3",
    ]
    afters = [q["after"] for q in feed_api.queries if "after" in q]
    assert afters[:2] == ["c0#run_0", "c2#run_2"]


def stop_after_sleeps(monkeypatch, module, n: int) -> list:
    """Records the bridge's sleeps; the one after the first `n` stops it (SystemExit)."""
    slept: list = []

    def sleep(seconds):
        if len(slept) == n:
            raise SystemExit
        slept.append(seconds)

    monkeypatch.setattr(module.time, "sleep", sleep)
    return slept


def test_backoff_honors_retry_after_up_to_a_minute_and_doubles_otherwise(bridge):
    module, _ = bridge
    assert module.backoff(failing(429, "7"), 1) == 7
    assert module.backoff(failing(429, "3600"), 1) == 60
    assert module.backoff(failing(503), 1) == module.POLL_S
    assert module.backoff(failing(503), 3) == module.POLL_S * 4
    assert module.backoff(failing(503), 50) == 300


@pytest.mark.parametrize(
    "error",
    [failing(429, "2"), failing(503), urllib.error.URLError("unreachable")],
    ids=["429", "503", "unreachable"],
)
def test_a_first_start_waits_out_an_api_that_does_not_answer(bridge, monkeypatch, capsys, error):
    module, yamcs = bridge
    serve = feed_api(workflow_runs("a"))
    errors = [error]

    def constellation(method, path, body=None):
        if errors and "page_records=1" in path:
            raise errors.pop()
        return serve(method, path, body)

    stop_after_sleeps(monkeypatch, module, 1)
    monkeypatch.setattr(module, "constellation", constellation)
    with pytest.raises(SystemExit):
        module.main()
    assert len(yamcs.events()) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_the_cursor_is_kept_in_state_dir_and_a_restart_resumes_from_it(bridge, monkeypatch):
    module, yamcs = bridge
    monkeypatch.setattr(module, "constellation", feed_api(workflow_runs("a")))
    stop_after_sleeps(monkeypatch, module, 0)
    with pytest.raises(SystemExit):
        module.main()
    assert Path(module.CURSOR_FILE).read_text() == "c1#run_1"
    assert ("POST", "/processors/simulator/realtime/parameters:batchSet") in [
        (m, p) for m, p, _ in yamcs.writes
    ]
    restarted, yamcs = load(monkeypatch)
    restarted.constellation = feed_api(workflow_runs("a"))
    monkeypatch.setattr(restarted, "saved_cursor", lambda: "c1#run_1")
    stop_after_sleeps(monkeypatch, restarted, 0)
    with pytest.raises(SystemExit):
        restarted.main()
    assert yamcs.events() == [] and feed_api.queries[0]["after"] == "c1#run_1"


def test_a_yamcs_restart_republishes_the_runs_the_bridge_kept_without_reading_the_feed_history(
    bridge, monkeypatch
):
    """Each workflow's newest run is kept in STATE_DIR as it is applied, so making again what
    Yamcs lost reads those runs by cursor, never the whole feed from its oldest run."""
    module, yamcs = bridge
    monkeypatch.setattr(module, "saved_cursor", lambda: "c0#run_0")
    monkeypatch.setattr(module, "constellation", feed_api(workflow_runs("a", "b", "a")))
    stop_after_sleeps(monkeypatch, module, 0)
    with pytest.raises(SystemExit):
        module.main()
    restarted, yamcs = load(monkeypatch)
    # Yamcs restarted without storage: it holds no cursor until the bridge sets it again.
    held = {"cursor": ""}
    monkeypatch.setattr(restarted, "saved_cursor", lambda: held["cursor"])
    monkeypatch.setattr(
        restarted, "set_parameters", lambda values: held.update(cursor=values.get("feed_cursor", held["cursor"]))
    )
    restarted.constellation = feed_api(workflow_runs("a", "b", "a"))
    stop_after_sleeps(monkeypatch, restarted, 0)
    with pytest.raises(SystemExit):
        restarted.main()
    applied = [json.loads(b)["run_id"] for _, p, b in yamcs.writes if "/objects/workflow-" in p]
    assert applied == ["run_2", "run_3"]
    assert all(q.get("after") != "" for q in feed_api.queries)


def test_a_yamcs_restart_skips_a_kept_run_the_feed_no_longer_holds(bridge, monkeypatch, capsys):
    """A workflow that has not run for 7 days: its newest run has expired, and the API answers
    its cursor 404. Making again what Yamcs lost skips it and goes on, so new runs still arrive."""
    module, yamcs = bridge
    monkeypatch.setattr(module, "saved_cursor", lambda: "c0#run_0")
    monkeypatch.setattr(module, "constellation", feed_api(workflow_runs("a", "b", "a")))
    stop_after_sleeps(monkeypatch, module, 0)
    with pytest.raises(SystemExit):
        module.main()
    restarted, yamcs = load(monkeypatch)
    held = {"cursor": ""}
    monkeypatch.setattr(restarted, "saved_cursor", lambda: held["cursor"])
    monkeypatch.setattr(
        restarted, "set_parameters", lambda values: held.update(cursor=values.get("feed_cursor", held["cursor"]))
    )
    serve = feed_api(workflow_runs("a", "b", "a", "b"))

    def constellation(method, path, body=None):
        if "cursor=c2" in path:
            raise urllib.error.HTTPError(API, 404, "Not Found", Message(), io.BytesIO(b"{}"))
        return serve(method, path, body)

    restarted.constellation = constellation
    stop_after_sleeps(monkeypatch, restarted, 0)
    with pytest.raises(SystemExit):
        restarted.main()
    applied = [json.loads(b)["run_id"] for _, p, b in yamcs.writes if "/objects/workflow-" in p]
    assert applied == ["run_3", "run_4"]
    assert "c2#run_2 is no longer in the feed" in capsys.readouterr().out


def test_a_yamcs_restart_while_the_bridge_runs_is_made_good_at_the_next_poll(bridge, monkeypatch):
    module, yamcs = bridge
    held = {"cursor": "c0#run_0"}
    monkeypatch.setattr(module, "saved_cursor", lambda: held["cursor"])
    monkeypatch.setattr(
        module, "set_parameters", lambda values: held.update(cursor=values.get("feed_cursor", held["cursor"]))
    )
    monkeypatch.setattr(module, "constellation", feed_api(workflow_runs("a")))
    slept: list = []

    def sleep(seconds):
        if len(slept) == 2:
            raise SystemExit
        if not slept:
            held["cursor"] = ""  # Yamcs restarted without storage after the first poll
        slept.append(seconds)

    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        module.main()
    applied = [json.loads(b)["run_id"] for _, p, b in yamcs.writes if "/objects/workflow-" in p]
    assert applied == ["run_1", "run_1"]


def test_outside_docker_state_is_in_the_users_state_directory_not_tmp(monkeypatch, tmp_path):
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", API)
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_KEY", "k")
    monkeypatch.delitem(os.environ, "STATE_DIR", raising=False)
    monkeypatch.delitem(os.environ, "ACTED_FILE", raising=False)
    monkeypatch.setitem(os.environ, "XDG_STATE_HOME", str(tmp_path))
    assert load(monkeypatch)[0].STATE_DIR == f"{tmp_path}/constellation"
    monkeypatch.delitem(os.environ, "XDG_STATE_HOME")
    monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
    module, _ = load(monkeypatch)
    assert module.CURSOR_FILE == f"{tmp_path}/.local/state/constellation/yamcs-bridge-cursor.txt"


def test_an_upgraded_bridge_moves_its_cursor_from_yamcs_to_state_dir(bridge, monkeypatch):
    module, yamcs = bridge
    monkeypatch.setattr(module, "saved_cursor", lambda: "c1#run_1")
    monkeypatch.setattr(module, "constellation", feed_api(workflow_runs("a")))
    stop_after_sleeps(monkeypatch, module, 0)
    with pytest.raises(SystemExit):
        module.main()
    assert Path(module.CURSOR_FILE).read_text() == "c1#run_1" and yamcs.events() == []


def test_a_read_only_state_dir_stops_the_bridge_at_start(bridge, monkeypatch, tmp_path, capsys):
    module, yamcs = bridge
    monkeypatch.setattr(module, "saved_cursor", lambda: pytest.fail("carried on"))
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(SystemExit) as stopped:
            module.main()
    finally:
        tmp_path.chmod(0o700)
    assert stopped.value.code == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert str(tmp_path) in line and "writable" in line


@pytest.mark.parametrize(
    "url, stops",
    [("http://example.com", True), ("http://127.0.0.1:8000", False), ("http://[::1]:8000", False)],
)
def test_plain_http_to_another_host_stops_at_start_before_the_key_is_sent(
    monkeypatch, tmp_path, capsys, url, stops
):
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", url)
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_KEY", "k")
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    monkeypatch.setitem(os.environ, "ACTED_FILE", str(tmp_path / "acted.txt"))
    module, yamcs = load(monkeypatch)
    monkeypatch.setattr(module, "writable", lambda: (_ for _ in ()).throw(SystemExit("started")))
    with pytest.raises(SystemExit) as stopped:
        module.main()
    if stops:
        assert stopped.value.code == 2
        (line,) = capsys.readouterr().err.splitlines()
        assert "not sending the API key to http://example.com" in line and "https" in line
    else:
        assert stopped.value.code == "started"
    assert yamcs.sent == [] and yamcs.writes == []


def test_a_missing_key_stops_at_start_with_one_line(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CONSTELLATION_API_URL", raising=False)
    monkeypatch.delenv("CONSTELLATION_API_KEY", raising=False)
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    module, _ = load(monkeypatch)
    assert module.API == "https://api.constellation.space"
    with pytest.raises(SystemExit) as exit:
        module.main()
    assert exit.value.code == 2
    assert capsys.readouterr().err.strip() == "bridge: CONSTELLATION_API_KEY is not set"
