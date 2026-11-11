"""The COSMOS plugin's bridge turns a run into CONSTELLATION packets: the next pass, the next
24 hours, the soonest swap and its minutes left, and into the documents its Constellation Forecast
tool reads. Feed entries come from the API's own encoding."""

import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import api
import pytest
from api import output, to_otlp

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bridges/cosmos/microservices/BRIDGE/bridge.py"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
CONSUMER = "cosmos-bridge"


class Bucket:
    """COSMOS's bucket client (openc3.utilities.bucket) over a dict, which outlives a restart."""

    def __init__(self, stored: dict[str, bytes] | None = None) -> None:
        self.stored = {} if stored is None else stored

    def get_object(self, bucket, key):
        raw = self.stored.get(f"{bucket}/{key}")
        return {"Body": io.BytesIO(raw)} if raw else None

    def put_object(self, bucket, key, body, content_type):
        assert content_type == "application/json"
        self.stored[f"{bucket}/{key}"] = body


def load(monkeypatch, name: str = "cosmos_bridge", stored: dict[str, bytes] | None = None):
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = Bucket(stored)
    monkeypatch.setattr(module, "bucket", lambda: (client, "config"))
    return module


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", "https://api.test")
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_KEY", "k")
    module = load(monkeypatch)
    injected: list[tuple[str, dict]] = []
    monkeypatch.setattr(module, "inject", lambda packet, values: injected.append((packet, values)))
    return module, injected


def item(start_h: float, p50: float, **extra) -> dict:
    """A pass item in the spec's shape."""
    at = NOW + timedelta(hours=start_h)
    gb = {"p10": p50 - 4, "p50": p50, "p90": p50 + 1}
    return {
        "entity": {
            "type": "pass",
            "id": f"p{start_h}",
            "link_id": "l1",
            "satellite_id": "sat-1",
            "ground_station_id": "gs",
            "antenna_id": "ant-1",
        },
        "target_at": at.isoformat(),
        "until": (at + timedelta(minutes=8)).isoformat(),
        "value": gb,
        "explain": {
            "expected_gb": {"value": gb, "unit": "GB", "source": "expected_gb"},
            "passes": {"value": 40.0, "unit": "deg", "source": "orbit"},
        },
        "workflow_version": 3,
        **extra,
    }


def swap(start_h: float, p50: float, minutes: int, to: str | None = "ant-2") -> dict:
    action = {
        "verb": "swap",
        "from": f"p{start_h}",
        "to": "p_b" if to else None,
        "to_antenna_id": to,
        "to_at": None,
        "deadline": (NOW + timedelta(minutes=minutes)).isoformat(),
        "mode": "suggest",
    }
    return item(start_h, p50, action=action, severity="warning")


def entry(*parts: tuple[list[dict], str], system: str = "cosmos") -> dict:
    """One feed entry for a run: a resourceLogs per feed output node, as the API encodes it."""
    # No template named: the bridge tells the kind of run by its items, as the contract allows.
    run = {"workflow": "w", "run_id": "run_1"}
    return {
        "resourceLogs": [
            log
            for items, connection in parts
            for log in to_otlp("t", items, NOW, run, output(connection, {"system": system}))[
                "resourceLogs"
            ]
        ]
    }


def test_the_next_pass_and_the_next_24_hours(bridge):
    module, _ = bridge
    out = module.packets([item(-1, 9), item(2, 10), item(5, 20), item(30, 99)], [], NOW)
    assert out["NEXT_PASS"]["AOS"] == "2026-11-06 14:00Z"
    assert out["NEXT_PASS"]["EXPECTED_GB_P50"] == 10 and out["NEXT_PASS"]["MAX_ELEVATION"] == 40.0
    assert out["NEXT_24H"] == {
        "PASSES": 2,
        "EXPECTED_GB_P10": 22.0,
        "EXPECTED_GB_P50": 30.0,
    }
    assert out["SWAP"] == module.NO_SWAP


def test_the_soonest_open_swap_with_minutes_left(bridge):
    module, _ = bridge
    swaps = [swap(3, 10, 120), swap(1, 10, 20, to=None), swap(0, 10, -5)]
    out = module.packets([], swaps, NOW)
    assert out["SWAP"]["MINUTES_TO_DEADLINE"] == 20.0 and out["SWAP"]["BACKUP_ANTENNA"] == "NONE"
    assert out["SWAP"]["OPEN"] == 2 and out["SWAP"]["KEEPS_GB_P10"] == 6


def test_passes_and_swaps_are_told_apart_by_the_items(bridge):
    module, _ = bridge
    link = {**item(2, 10), "entity": {"type": "link", "id": "l1"}}
    planned = item(4, 10, series=[{"t": NOW.isoformat(), "value": {"modcod": 3}}])
    items = [item(2, 10), swap(3, 10, 60), link, planned]
    assert [i["entity"]["id"] for i in module.priced_passes(items)] == ["p2"]
    assert [i["entity"]["id"] for i in module.swap_items(items)] == ["p3"]


def test_only_the_resource_logs_for_this_connection_are_read(bridge):
    module, injected = bridge
    otlp = entry(([item(2, 10)], CONSUMER), ([item(5, 20), item(6, 30)], "yamcs-bridge"))
    [(resource, items)] = module.results.unpack(otlp, CONSUMER)
    assert resource["constellation.connection"] == CONSUMER and len(items) == 1
    assert module.output_of(resource)["widget"]["on"]
    forecast = module.Forecast()
    forecast.take(resource, items)
    assert forecast.publish(NOW) == 4
    assert [p for p, _ in injected] == ["NEXT_24H", "SWAP", "NEXT_PASS", "RUN"]
    assert dict(injected)["RUN"]["WORKFLOW_VERSION"] == 3
    assert module.results.unpack(entry(([item(2, 10)], "yamcs-bridge")), CONSUMER) == []


def test_a_swap_run_replaces_the_swaps_but_keeps_the_passes(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    forecast.take(*module.results.unpack(entry(([item(2, 10)], CONSUMER)), CONSUMER)[0])
    forecast.take(*module.results.unpack(entry(([swap(3, 10, 60)], CONSUMER)), CONSUMER)[0])
    assert len(forecast.passes) == 1 and len(forecast.swaps) == 1
    forecast.take(*module.results.unpack(entry(([item(5, 20), item(6, 20)], CONSUMER)), CONSUMER)[0])
    assert len(forecast.passes) == 2 and len(forecast.swaps) == 1


def test_a_run_becomes_its_workflow_document_and_an_index_entry(bridge):
    module, _ = bridge
    [(resource, items)] = module.results.unpack(entry(([item(2, 10)], CONSUMER)), CONSUMER)
    earlier = {"workflows": [{"workflow": "a-other"}, {"workflow": "w", "run_id": "old"}]}
    docs = module.documents(resource, items, ["tiles", "list"], earlier, NOW)
    run = docs["workflow-w.json"]
    assert run["items"] == items and run["blocks"] == ["tiles", "list"] and run["count"] == 1
    assert run["kind"] == "pass-yield" and run["delivered_at"] == NOW.isoformat()
    listed = docs["index.json"]["workflows"]
    assert [w["workflow"] for w in listed] == ["a-other", "w"]
    assert listed[1] == {k: v for k, v in run.items() if k != "items"}


def test_the_documents_land_in_the_config_bucket_under_the_scope(bridge, monkeypatch):
    module, _ = bridge
    stored: dict[str, bytes] = {"config/DEFAULT/constellation/index.json": b'{"workflows": []}'}

    class Client:
        def get_object(self, bucket, key):
            raw = stored.get(f"{bucket}/{key}")
            return {"Body": type("B", (), {"read": lambda _: raw})()} if raw else None

        def put_object(self, bucket, key, body, content_type):
            assert content_type == "application/json"
            stored[f"{bucket}/{key}"] = body

    monkeypatch.setattr(module, "bucket", lambda: (Client(), "config"))
    module.publish_documents(
        *module.results.unpack(entry(([item(2, 10)], CONSUMER)), CONSUMER)[0], ["tiles"]
    )
    assert set(stored) == {
        "config/DEFAULT/constellation/index.json",
        "config/DEFAULT/constellation/workflow-w.json",
    }
    assert (
        json.loads(stored["config/DEFAULT/constellation/index.json"])["workflows"][0]["count"] == 1
    )


def test_the_bridge_and_the_yamcs_bridge_write_the_same_documents(bridge, monkeypatch):
    """The Constellation Forecast view reads the same documents in COSMOS as in Yamcs."""
    module, _ = bridge
    path = ROOT / "clients/python/src/constellation_space/bridges/yamcs/bridge.py"
    spec = importlib.util.spec_from_file_location("yamcs_bridge_for_parity", path)
    assert spec and spec.loader
    yamcs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(yamcs)
    index = {"workflows": [{"workflow": "a-other", "template": "audit-ledger"}]}
    written: dict[str, dict] = {}

    def fake(method, path, body=None, raw=None, kind=""):
        if path == "/storage/buckets":
            return {"buckets": [{"name": yamcs.BUCKET}]}
        if method == "GET":
            return index
        assert raw is not None
        written[path.rsplit("/", 1)[1]] = json.loads(raw)
        return None

    monkeypatch.setattr(yamcs, "yamcs", fake)
    [(resource, items)] = module.results.unpack(
        entry(([item(2, 10), item(5, 20)], CONSUMER)), CONSUMER
    )
    blocks = ["tiles", "chart", "list"]
    yamcs.publish_widget(resource, items, {"on": True, "blocks": blocks})
    ours = module.documents(resource, items, blocks, index, NOW)
    for doc in (*written.values(), ours["workflow-w.json"], *ours["index.json"]["workflows"]):
        doc.pop("delivered_at", None)
    for w in written["index.json"]["workflows"]:
        w.pop("delivered_at", None)
    assert written == ours


TOOL = SOURCE.parents[2] / "tools/constellationforecast/main.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_tool_registers_single_spa_lifecycles_and_reads_cosmos_storage():
    """COSMOS imports the tool with SystemJS and mounts it with single-spa: main.js must
    System.register bootstrap, mount and unmount. load() reads a document through COSMOS's
    storage API with the operator's token and decodes it as UTF-8."""
    doc = {"workflows": [{"workflow": "w", "template": "pass-yield", "note": "p50 · GB"}]}
    contents = base64.encodebytes(json.dumps(doc, ensure_ascii=False).encode()).decode()
    script = f"""
const vm = require('vm'), fs = require('fs');
let exported;
const calls = [];
const sandbox = {{
  System: {{ register: (deps, declare) => declare((e) => (exported = e)).execute() }},
  window: {{ openc3Scope: 'DEFAULT' }}, localStorage: {{ openc3Token: 'tok' }},
  atob, TextDecoder, Uint8Array, encodeURIComponent, JSON,
  fetch: async (url, opts) => {{
    calls.push([url, opts.headers.Authorization]);
    if (url.includes('missing')) return {{ ok: false, status: 404 }};
    return {{ ok: true, json: async () => ({{ contents: {json.dumps(contents)} }}) }};
  }},
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(TOOL))}, 'utf8'), sandbox);
(async () => {{
  const doc = await exported.load('index.json');
  let missing;
  try {{ await exported.load('missing.json'); }} catch (e) {{ missing = e.message; }}
  const kinds = ['bootstrap', 'mount', 'unmount'].map((k) => typeof exported[k]);
  console.log(JSON.stringify({{ kinds, doc, missing, calls }}));
}})();
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    assert got["kinds"] == ["function"] * 3
    assert got["doc"] == doc and got["missing"] == "none yet"
    assert got["calls"][0] == [
        "/openc3-api/storage/download_file/DEFAULT%2Fconstellation%2Findex.json"
        "?bucket=OPENC3_CONFIG_BUCKET&scope=DEFAULT",
        "tok",
    ]


def test_items_in_a_shape_newer_than_the_bridge_reads_are_skipped_not_misread(
    bridge, monkeypatch, capsys
):
    module, _ = bridge
    monkeypatch.setattr(api, "SCHEMA_VERSION", "constellation.item/v3")
    assert module.results.unpack(entry(([item(2, 10)], CONSUMER)), CONSUMER) == []
    assert "constellation.item/v3" in capsys.readouterr().out


def test_the_api_password_is_set_before_anything_imports_cosmos(monkeypatch):
    """Writing the Forecast tool's documents imports COSMOS before the first inject; its API
    client signs in with OPENC3_API_PASSWORD as it was then, so the bridge sets it on start."""
    monkeypatch.delenv("OPENC3_API_PASSWORD", raising=False)
    monkeypatch.setenv("OPENC3_SERVICE_PASSWORD", "service-secret")
    spec = importlib.util.spec_from_file_location("cosmos_bridge_start", SOURCE)
    assert spec and spec.loader
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert os.environ["OPENC3_API_PASSWORD"] == "service-secret"


def test_a_run_that_fails_part_way_is_tried_again_not_skipped(bridge, monkeypatch):
    module, _ = bridge
    run = {"cursor": "20261106T000001.000000#run_1", "otlp": entry(([item(2, 10)], CONSUMER))}
    afters, reported, failures = [], [], [RuntimeError("COSMOS refused the inject")]

    def call(method, path, body=None):
        if path.startswith("/predictions/feed?"):
            query = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
            if query.get("from") == "latest":
                return {"entries": [], "next": api.start([run])}
            after = query.get("after", "")
            if query["limit"] == "1":
                afters.append(after)
            entries = [run] if after < run["cursor"] else []
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries" and body:
            reported.append(body["run_id"])
        return None

    def inject(packet, values):
        if failures:
            raise failures.pop()

    sleeps = iter(range(3))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", inject)
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    # The failed attempt left the cursor where it was: the same run was asked for again.
    assert afters[:2] == ["", ""] and reported == ["run_1"]


def capacity_day(antenna: str, day: datetime, p50: float, passes: int = 3) -> dict:
    value = {
        "passes": passes,
        "sellable_min": 7.5 * passes,
        "p10": p50 * 0.8,
        "p50": p50,
        "p90": p50 * 1.1,
    }
    return {
        "entity": {"type": "antenna", "id": antenna, "ground_station_id": "gs-1"},
        "target_at": day.isoformat(),
        "until": (day + timedelta(days=1)).isoformat(),
        "value": value,
        "explain": {"sum_by": {"value": value, "unit": "GB", "source": "sum_by"}},
        "workflow_version": 2,
    }


def test_a_capacity_run_with_only_p50_does_not_crash_and_shows_no_p10(bridge):
    module, _ = bridge
    days = [capacity_day("ant-1", TODAY, 40.0), capacity_day("ant-2", TODAY, 12.5)]
    for day in days:
        del day["value"]["p10"], day["value"]["p90"]
    capacity = module.capacity_packet(days, NOW)
    assert capacity["GB_P50"] == 52.5 and capacity["GB_P10"] == 0.0
    assert capacity["TIGHTEST_ANTENNA"] == "ant-2"


def plan(start_h: float, *steps: tuple[str, float]) -> dict:
    planned = item(start_h, 10)
    aos = datetime.fromisoformat(planned["target_at"])
    series = [
        {"t": (aos + timedelta(minutes=k)).isoformat(), "value": {"modcod": m, "sinr_db": s}}
        for k, (m, s) in enumerate(steps)
    ]
    return {**planned, "series": series}


TODAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)


def test_capacity_and_modcod_packets_say_what_operators_need(bridge):
    module, _ = bridge
    days = [
        capacity_day("ant-1", TODAY, 40.0),
        capacity_day("ant-2", TODAY, 12.5, passes=1),
        capacity_day("ant-1", TODAY + timedelta(days=1), 99.0),
    ]
    plans = [
        plan(5, ("QPSK 1/2", 1.2), ("8PSK 2/3", 7.9)),
        plan(2, ("16APSK 3/4", 10.9), ("8PSK 2/3", 6.1), ("QPSK 1/2", 0.4)),
        plan(-3, ("QPSK 1/2", 1.0)),
    ]
    capacity = module.capacity_packet(days, NOW)
    assert capacity == {
        "DAY": TODAY.date().isoformat(),
        "ANTENNAS": 2,
        "PASSES": 4,
        "SELLABLE_MIN": 30.0,
        "GB_P10": 42.0,
        "GB_P50": 52.5,
        "TIGHTEST_ANTENNA": "ant-2",
        "TIGHTEST_GB_P50": 12.5,
    }
    modcod = module.modcod_packet(plans, NOW)
    assert (
        modcod["AOS"],
        modcod["CHANGES"],
        modcod["FIRST"],
        modcod["LAST"],
        modcod["MIN_SINR_DB"],
        modcod["PLANNED_PASSES"],
    ) == (
        module._minute(plans[1]["target_at"]),
        2,
        "16APSK 3/4",
        "QPSK 1/2",
        0.4,
        2,
    )
    assert module.capacity_packet([], NOW) is None and module.modcod_packet([], NOW) is None


def _resource(template: str) -> dict:
    return {
        "constellation.workflow": "w",
        "constellation.template": template,
        "constellation.run_id": "run_x",
        "constellation.workflow_version": 1,
    }


def test_each_kind_of_run_replaces_only_its_own_packets_and_an_empty_one_clears_them(bridge):
    module, injected = bridge
    forecast = module.Forecast()
    forecast.take(_resource("pass-yield"), [item(2, 10)])
    forecast.take(_resource("rain-fade-swap"), [swap(3, 10, 60)])
    forecast.take(_resource("antenna-capacity"), [capacity_day("ant-1", TODAY, 40.0)])
    forecast.take(_resource("modcod-plan"), [plan(2, ("QPSK 1/2", 1.2), ("8PSK 2/3", 7.9))])
    forecast.publish(NOW)
    sent = dict(injected)
    assert sent["NEXT_24H"]["PASSES"] == 1 and sent["SWAP"]["OPEN"] == 1
    assert sent["CAPACITY"]["GB_P50"] == 40.0 and sent["MODCOD"]["CHANGES"] == 1
    # A later swap run that advises nothing takes the swap down and leaves the rest.
    injected.clear()
    forecast.take(_resource("rain-fade-swap"), [])
    forecast.publish(NOW)
    sent = dict(injected)
    assert sent["SWAP"] == module.NO_SWAP and sent["NEXT_24H"]["PASSES"] == 1
    assert sent["RUN"]["TEMPLATE"] == "rain-fade-swap" and sent["RUN"]["RESULTS"] == 0


# --- the API key ------------------------------------------------------------------------------


class Secrets:
    """COSMOS's secret store (Admin > Secrets) as openc3.utilities.secrets serves it: get() is
    None for a secret never set."""

    key: str | None = None
    scopes: list[str | None] = []

    @classmethod
    def get_client(cls):
        return cls()

    def get(self, key, secret_store=None, scope=None):
        Secrets.scopes.append(scope)
        return Secrets.key if key == "CONSTELLATION_API_KEY" else None


def polled(
    monkeypatch,
    bridge,
    sleeps: int,
    rotate=None,
    refused: dict[str, int] | None = None,
    answer: bytes = b"{}",
) -> tuple[list, list]:
    """Runs the bridge's loop through `sleeps` sleeps against a feed with nothing new. Returns the
    key each request carried (path, key) and the sleeps. `rotate(n)` runs at the
    n-th sleep; a key in `refused` gets its HTTP status, with `answer` as the body."""
    module, _ = bridge
    sent, slept = [], []

    def urlopen(req, timeout=None):
        key = req.get_header("X-api-key")
        sent.append((urllib.parse.urlsplit(req.full_url).path, key))
        if key in (refused or {}):
            status = (refused or {})[key]
            raise urllib.error.HTTPError(req.full_url, status, "", {}, io.BytesIO(answer))  # type: ignore[arg-type]
        return io.BytesIO(b'{"entries": [], "next": ""}')

    def sleep(seconds):
        slept.append(seconds)
        if rotate:
            rotate(len(slept))
        if len(slept) == sleeps:
            raise StopIteration

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    return sent, slept


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(Secrets, "key", None)
    monkeypatch.setattr(Secrets, "scopes", [])
    monkeypatch.setitem(
        sys.modules, "openc3.utilities.secrets", types.SimpleNamespace(Secrets=Secrets)
    )
    return Secrets


def test_the_key_is_read_from_cosmos_secrets_each_poll_so_a_changed_key_needs_no_reinstall(
    monkeypatch, secrets
):
    monkeypatch.setenv("CONSTELLATION_API_URL", "https://api.test")
    monkeypatch.setenv("OPENC3_SCOPE", "OPS")
    module = load(monkeypatch, "cosmos_bridge_ops")
    secrets.key = "first"
    sent, _ = polled(
        monkeypatch, (module, []), 2, rotate=lambda n: setattr(Secrets, "key", "second")
    )
    feed = [key for path, key in sent if path == "/predictions/feed"]
    # The first start's look for the newest run, then one poll per key.
    assert feed == ["first", "first", "second"]
    # The new key checks in again, so Integrations sees the bridge with it.
    assert [key for path, key in sent if path.endswith("/checkin")] == ["first", "second"]
    assert set(secrets.scopes) == {"OPS"}


def test_outside_cosmos_the_key_comes_from_the_environment(bridge, monkeypatch):
    monkeypatch.setitem(sys.modules, "openc3.utilities.secrets", None)
    sent, _ = polled(monkeypatch, bridge, 1)
    assert sent and {key for _, key in sent} == {"k"}


def test_without_a_key_the_bridge_waits_and_says_so_once_a_minute(
    bridge, monkeypatch, secrets, capsys
):
    monkeypatch.delenv("CONSTELLATION_API_KEY")
    sent, slept = polled(
        monkeypatch, bridge, 3, rotate=lambda n: n == 2 and setattr(Secrets, "key", "added")
    )
    assert slept[:2] == [60, 60]
    out = capsys.readouterr()
    assert (out.out + out.err).count("No CONSTELLATION_API_KEY in Admin > Secrets yet") == 2
    # Once added, it is used without a restart.
    assert ("/predictions/feed", "added") in sent and all(key == "added" for _, key in sent)


def test_a_rejected_key_is_said_plainly_and_the_bridge_waits_for_a_new_one(
    bridge, monkeypatch, secrets, capsys
):
    secrets.key = "revoked"
    sent, slept = polled(
        monkeypatch,
        bridge,
        3,
        rotate=lambda n: n == 2 and setattr(Secrets, "key", "fresh"),
        refused={"revoked": 401},
    )
    out = capsys.readouterr()
    logged = out.out + out.err
    assert logged.count("Constellation does not accept CONSTELLATION_API_KEY") == 1
    assert "predictions:read" in logged and "Traceback" not in logged
    assert slept[:2] == [60, 60]
    assert ("/predictions/feed", "fresh") in sent


def test_a_plan_refusal_is_said_once_and_the_bridge_stops_polling_until_the_key_changes(
    bridge, monkeypatch, secrets, capsys
):
    secrets.key = "below-plan"
    plan = {"error": "plan_required", "plan": "enterprise", "feature": "output:cosmos"}
    sent, slept = polled(
        monkeypatch,
        bridge,
        4,
        rotate=lambda n: n == 3 and setattr(Secrets, "key", "upgraded"),
        refused={"below-plan": 403},
        answer=json.dumps({"detail": plan}).encode(),
    )
    # One refused check-in, then no call to Constellation with that key: only Admin > Secrets.
    assert [path for path, key in sent if key == "below-plan"] == ["/predictions/feed/checkin"]
    assert slept[:3] == [60, 60, 60]
    out = capsys.readouterr()
    assert (out.out + out.err).count("plan does not include output:cosmos") == 1
    assert ("/predictions/feed", "upgraded") in sent


def test_an_api_outage_is_logged_and_polled_again_not_taken_for_a_bad_key(
    bridge, monkeypatch, secrets, capsys
):
    secrets.key = "good"
    sent, slept = polled(monkeypatch, bridge, 2, refused={"good": 503})
    module, _ = bridge
    assert slept == [module.POLL_S, module.POLL_S]
    assert "rejected" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "status, answer, says, wait",
    [
        (429, {"error": "auth_lockout", "retry_after_seconds": 1200}, "locked out", 1200),
        (429, {"error": "rate_limited", "retry_after_seconds": 30}, "HTTP 429 rate_limited", 60),
        (402, {"detail": {"error": "upgrade_required"}}, "HTTP 402 upgrade_required", 3600),
    ],
)
def test_a_lockout_or_hold_is_said_once_and_waited_out_not_hammered(
    bridge, monkeypatch, secrets, capsys, status, answer, says, wait
):
    secrets.key = "held"
    sent, slept = polled(
        monkeypatch, bridge, 3, refused={"held": status}, answer=json.dumps(answer).encode()
    )
    assert slept == [wait, wait, wait]
    out = capsys.readouterr()
    assert (out.out + out.err).count(says) == 1
    assert len([p for p, _ in sent if p == "/predictions/feed"]) == 3


# --- where the feed is read from --------------------------------------------------------------


def cursor_of(n: int) -> str:
    return f"20261106T00000{n}.000000#run_{n}"


def fed(*entries: tuple[int, list[dict]]) -> list[dict]:
    """Feed entries as the API serves them to this connection, one per (n, items)."""
    return [{"cursor": cursor_of(n), "otlp": entry((items, CONSUMER))} for n, items in entries]


def ran(monkeypatch, feed: list[dict], stored: dict[str, bytes], oldest: str | None = None):
    """A freshly started bridge against `feed`, run until it polls with nothing new. Returns
    what it injected, the runs it reported and each poll's `after`."""
    module = load(monkeypatch, stored=stored)
    injected, reported, afters = [], [], []

    def call(method, path, body=None):
        if path.startswith("/predictions/feed?"):
            query = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
            if query.get("from") == "latest":
                return {"entries": [], "next": api.start(feed), "oldest": oldest}
            after = query.get("after", "")
            if query["limit"] == "1":
                afters.append(after)
            entries = [e for e in feed if e["cursor"] > after][: int(query["limit"])]
            nxt = entries[-1]["cursor"] if entries else after
            return {"entries": entries, "next": nxt, "oldest": oldest}
        if path == "/predictions/feed/deliveries" and body:
            reported.append(body["run_id"])
        return None

    def sleep(seconds):
        raise StopIteration

    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: injected.append((packet, values)))
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    return injected, reported, afters


def test_an_output_for_another_system_is_refused_and_the_rest_of_the_run_applied(
    bridge, monkeypatch, capsys
):
    """As the Yamcs and Lattice bridges do: an output to this connection for another system (a
    Yamcs output, as one saved without a mapping is) is skipped, logged and reported as refused,
    and the feed moves past it."""
    yamcs = entry(([item(3, 99), item(4, 99)], CONSUMER), system="yamcs")["resourceLogs"]
    both = entry(([item(2, 10)], CONSUMER))
    both["resourceLogs"] += yamcs
    feed = [{"cursor": cursor_of(1), "otlp": both}]
    module = load(monkeypatch)
    reports = []

    def call(method, path, body=None):
        if "from=latest" in path:
            return {"entries": [], "next": api.start(feed)}
        if path.startswith("/predictions/feed?"):
            after = dict(urllib.parse.parse_qsl(path.split("?", 1)[1])).get("after", "")
            entries = [e for e in feed if e["cursor"] > after]
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries":
            reports.append(body)
        return None

    injected = []
    sleeps = iter(range(1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: injected.append((packet, values)))
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    (report,) = reports
    assert report["applied"]["refused"] == 1 and report["applied"]["forecasts"] == 1
    assert dict(injected)["RUN"]["RESULTS"] == 1
    assert "it is a yamcs output, not a COSMOS one" in capsys.readouterr().out


def test_a_resource_tagged_for_another_system_is_refused_whatever_its_output_says(
    bridge, monkeypatch, capsys
):
    otlp = api.tagged(entry(([item(2, 10)], CONSUMER)), "yamcs")
    module = load(monkeypatch)
    reports = []

    def call(method, path, body=None):
        if "from=latest" in path:
            return {"entries": [], "next": ""}
        if path.startswith("/predictions/feed?"):
            after = dict(urllib.parse.parse_qsl(path.split("?", 1)[1])).get("after", "")
            entries = [{"cursor": cursor_of(1), "otlp": otlp}] if after < cursor_of(1) else []
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries":
            reports.append(body["applied"])
        return None

    sleeps = iter(range(1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: None)
    monkeypatch.setattr(module, "publish_documents", lambda *a: pytest.fail("applied"))
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    (report,) = reports
    assert (report["forecasts"], report["refused"]) == (0, 1)
    assert "it is a yamcs output, not a COSMOS one" in capsys.readouterr().out


def test_a_first_start_applies_only_the_newest_run(bridge, monkeypatch):
    feed = fed((1, [item(2, 10)]), (2, [item(3, 20)]))
    injected, reported, _ = ran(monkeypatch, feed, {})
    assert reported == ["run_2"]
    # One run injected: never the older one, stamped now.
    assert [p for p, _ in injected].count("RUN") == 1


# The bridge's loop reads the wall clock: hours from NOW to a while after it.
AHEAD_H = (datetime.now(UTC) - NOW).total_seconds() / 3600 + 3


def test_a_restart_resumes_after_its_cursor_with_the_forecast_it_held(bridge, monkeypatch):
    stored: dict[str, bytes] = {}
    feed = fed((1, [item(2, 10)]), (2, [swap(AHEAD_H, 20, minutes=int(AHEAD_H * 60))]))
    ran(monkeypatch, feed, stored)
    feed += fed((3, [item(4, 30)]))
    injected, reported, afters = ran(monkeypatch, feed, stored)
    assert afters[0] == cursor_of(2) and reported == ["run_3"]
    # The swap run 2 advised is still open after the restart; a pass run leaves it alone.
    assert [v["OPEN"] for p, v in injected if p == "SWAP"] == [1]


def test_a_cursor_older_than_the_feed_holds_says_runs_expired_and_reads_on(
    bridge, monkeypatch, capsys
):
    stored: dict[str, bytes] = {}
    ran(monkeypatch, fed((1, [item(2, 10)])), stored)
    _, reported, afters = ran(monkeypatch, fed((5, [item(3, 20)])), stored, oldest=cursor_of(5))
    assert afters[0] == cursor_of(1) and reported == ["run_5"]
    err = capsys.readouterr().err
    assert "expired" in err and cursor_of(1) in err and cursor_of(5) in err


def test_a_malformed_item_is_skipped_and_never_holds_up_the_runs_after_it(
    bridge, monkeypatch, capsys
):
    stored: dict[str, bytes] = {}
    ran(monkeypatch, fed((1, [item(2, 10)])), stored)
    bad = {**item(3, 20), "target_at": "soon"}
    feed = fed((1, [item(2, 10)]), (2, [bad, item(4, 20)]), (3, [item(5, 30)]))
    _, reported, afters = ran(monkeypatch, feed, stored)
    assert reported == ["run_2", "run_3"] and afters[-1] == cursor_of(3)
    assert "skipped item 0 of run run_1: it is malformed" in capsys.readouterr().out


def test_a_failed_check_in_is_tried_again_until_it_succeeds(bridge, monkeypatch, secrets):
    secrets.key = "good"
    failed = []

    def refuse_first_checkin(req, timeout=None):
        if req.full_url.endswith("/checkin") and not failed:
            failed.append(True)
            raise urllib.error.HTTPError(req.full_url, 503, "", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        return io.BytesIO(b'{"entries": []}')

    module, _ = bridge
    sent, slept = [], []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise StopIteration

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda req, timeout=None: sent.append(req.full_url) or refuse_first_checkin(req),
    )
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    assert len([u for u in sent if u.endswith("/checkin")]) == 2


def test_the_bridge_checks_in_again_every_checkin_s(bridge, monkeypatch, secrets):
    secrets.key = "good"
    module, _ = bridge
    sent: list[str] = []
    clock = [0.0]

    def sleep(seconds):
        clock[0] += 200
        if clock[0] > 700:
            raise StopIteration

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda req, timeout=None: sent.append(req.full_url) or io.BytesIO(b'{"entries": []}'),
    )
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    assert len([u for u in sent if u.endswith("/checkin")]) == 2


def approved(start_h: float, p50: float, minutes: int, to: str | None = "ant-2") -> dict:
    s = swap(start_h, p50, minutes, to)
    return {**s, "action": {**s["action"], "mode": "approve"}, "action_status": "approved"}


def held(start_h: float, p50: float) -> dict:
    return item(start_h, p50, severity="warning", action_status="pending")


def test_the_swap_packet_says_whether_the_swap_shown_is_approved_and_counts_held_ones(bridge):
    module, _ = bridge
    out = module.packets([], [approved(1, 10, 20), swap(3, 10, 120)], NOW, pending=2)
    assert out["SWAP"]["STATUS"] == "APPROVED" and out["SWAP"]["MINUTES_TO_DEADLINE"] == 20.0
    assert (out["SWAP"]["OPEN"], out["SWAP"]["APPROVED"], out["SWAP"]["PENDING"]) == (2, 1, 2)
    out = module.packets([], [swap(1, 10, 20)], NOW)
    assert (out["SWAP"]["STATUS"], out["SWAP"]["APPROVED"], out["SWAP"]["PENDING"]) == (
        "ADVISED",
        0,
        0,
    )


def test_with_no_swap_open_the_packet_still_counts_what_is_held(bridge):
    module, _ = bridge
    out = module.packets([], [], NOW, pending=3)
    assert out["SWAP"] == {**module.NO_SWAP, "PENDING": 3} and out["SWAP"]["STATUS"] == "NONE"


def test_an_approved_swap_without_a_backup_reads_none(bridge):
    module, _ = bridge
    out = module.packets([], [approved(1, 10, 20, to=None)], NOW)
    assert out["SWAP"]["BACKUP_ANTENNA"] == "NONE" and out["SWAP"]["STATUS"] == "APPROVED"


def test_a_held_swap_run_counts_its_held_items_and_never_takes_them_for_passes(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    forecast.take(_resource("pass-yield"), [item(2, 10)])
    forecast.take(_resource("rain-fade-swap"), [held(3, 10), held(4, 10), approved(5, 10, 60)])
    assert forecast.pending == 2 and [s["entity"]["id"] for s in forecast.swaps] == ["p5"]
    assert [p["entity"]["id"] for p in forecast.passes] == ["p2"]
    assert module.priced_passes([held(3, 10)]) == []
    # A state saved before held swaps were counted restores with none held.
    restored = module.Forecast()
    restored.restore({k: v for k, v in forecast.saved().items() if k != "pending"})
    assert restored.pending == 0


def test_a_run_naming_its_template_is_documented_as_that_kind(bridge):
    module, _ = bridge
    docs = module.documents(_resource("rain-fade-swap"), [held(3, 10)], ["tiles"], {}, NOW)
    assert docs["workflow-w.json"]["kind"] == "rain-fade-swap"


def test_every_swap_value_is_an_item_of_the_swap_packet(bridge):
    module, _ = bridge
    tlm = (
        ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt"
    ).read_text()
    block = tlm.split("TELEMETRY CONSTELLATION SWAP ", 1)[1].split("\nTELEMETRY ", 1)[0]
    items = [
        line.split()[1] for line in block.splitlines() if line.strip().startswith("APPEND_ITEM")
    ]
    assert list(module.NO_SWAP) == items
    assert items[-3:] == ["STATUS", "APPROVED", "PENDING"]


def test_a_held_action_sent_with_its_body_is_counted_held_not_shown(bridge):
    module, _ = bridge
    with_body = {**swap(3, 10, 60), "action_status": "pending"}
    assert module.swap_items([with_body]) == []
    forecast = module.Forecast()
    forecast.take(_resource("rain-fade-swap"), [with_body])
    assert forecast.swaps == [] and forecast.pending == 1


def renamed(it: dict) -> dict:
    """The item as a copied workflow whose expected-GB node has another id writes it."""
    explain = dict(it["explain"])
    return {**it, "explain": {"gb_node": explain.pop("expected_gb"), **explain}}


def test_expected_gb_is_read_from_the_item_whatever_node_id_its_ledger_uses(bridge):
    module, _ = bridge
    passes = module.priced_passes([renamed(item(2, 10))])
    assert len(passes) == 1
    assert module.packets(passes, [renamed(swap(1, 10, 20))], NOW)["SWAP"]["EXPECTED_GB_P50"] == 10


def test_a_swap_with_no_expected_gb_is_skipped_and_logged_once_not_retried(bridge, capsys):
    module, _ = bridge
    bare = {k: v for k, v in swap(1, 10, 20).items() if k not in ("value", "explain")}
    forecast = module.Forecast()
    forecast.take({"constellation.template": "rain-fade-swap"}, [bare, swap(2, 10, 30)])
    assert len(forecast.swaps) == 1
    assert forecast.publish(NOW) > 0
    assert capsys.readouterr().out.count("no expected GB") == 1


def test_a_run_reports_the_approved_swaps_it_shows_as_acted(bridge, monkeypatch):
    feed = fed((1, [approved(2, 10, 30), swap(3, 10, 30), approved(4, 10, -5)]))
    module = load(monkeypatch)
    monkeypatch.setattr(module.results, "now", lambda: NOW)
    reports = []

    def call(method, path, body=None):
        if path.startswith("/predictions/feed?"):
            after = dict(urllib.parse.parse_qsl(path.split("?", 1)[1])).get("after", "")
            entries = [e for e in feed if e["cursor"] > after][:1]
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries":
            reports.append(body["applied"])
        return None

    sleeps = iter(range(1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: None)
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module, "resume", lambda consumer, forecast: "")
    monkeypatch.setattr(module, "keep", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    (report,) = reports
    assert report["acted"] == 1


def test_a_standing_approval_is_reported_acted_once_across_runs_and_kept_for_a_restart(
    bridge, monkeypatch
):
    approval = {**approved(2, 10, 30), "decision_id": "d1"}
    feed = fed((1, [approval]), (2, [approval]))
    module = load(monkeypatch)
    monkeypatch.setattr(module.results, "now", lambda: NOW)
    reports, kept = [], []

    def call(method, path, body=None):
        if path.startswith("/predictions/feed?"):
            after = dict(urllib.parse.parse_qsl(path.split("?", 1)[1])).get("after", "")
            entries = [e for e in feed if e["cursor"] > after][:1]
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries":
            reports.append(body["applied"])
        return None

    sleeps = iter(range(1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: None)
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module, "resume", lambda consumer, forecast: "")
    monkeypatch.setattr(module, "keep", lambda c, cur, forecast: kept.append(forecast.saved()))
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    assert [r.get("acted") for r in reports] == [1, None]
    restarted = module.Forecast()
    restarted.restore(kept[-1])
    assert module.results.first_acted(module.swap_items([approval]), restarted.acted) == 0


@pytest.mark.parametrize(
    "url, stops",
    [("http://example.com", True), ("http://127.0.0.1:8000", False), ("https://api.test", False)],
)
def test_plain_http_to_another_host_stops_at_start_before_the_key_is_sent(
    bridge, monkeypatch, capsys, url, stops
):
    module, _ = bridge
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", url)
    monkeypatch.setattr(module, "api_key", lambda: (_ for _ in ()).throw(SystemExit("started")))
    monkeypatch.setattr(module.results, "http", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(SystemExit) as stopped:
        module.main()
    if stops:
        assert stopped.value.code == 2
        (line,) = capsys.readouterr().err.splitlines()
        assert "not sending the API key to http://example.com" in line and "https" in line
    else:
        assert stopped.value.code == "started"
