"""The COSMOS plugin's bridge turns a run into CONSTELLATION packets: the next pass, the next
24 hours, the soonest swap and its minutes left, and into the documents its Constellation Forecast
tool reads. Feed entries come from the API's own encoding."""

import base64
import importlib.util
import io
import json
import os
import re
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
        "AT_RISK": 0,
    }
    assert out["SWAP"] == module.NO_SWAP


def test_the_next_pass_is_the_next_aos_not_a_pass_already_under_way(bridge):
    module, _ = bridge
    # A day-long link (a GEO satellite) started an hour ago: it counts in the day, it is not next.
    under_way = {**item(-1, 160), "until": (NOW + timedelta(hours=23)).isoformat()}
    out = module.packets([under_way, item(2, 10)], [], NOW)
    assert out["NEXT_PASS"]["AOS"] == "2026-11-06 14:00Z"
    assert out["NEXT_24H"]["PASSES"] == 2


def test_with_no_pass_ahead_the_next_pass_reads_none_not_the_last_one(bridge):
    module, _ = bridge
    out = module.packets([item(-1, 9)], [], NOW)
    assert out["NEXT_PASS"] == module.NO_PASS and out["NEXT_PASS"]["SATELLITE"] == "NONE"


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
    # A pass run: the pass packets and RUN, none of a kind no run has brought.
    assert forecast.publish(NOW) == 3
    assert [p for p, _ in injected] == ["NEXT_24H", "NEXT_PASS", "RUN"]
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
    storage API with the operator's token and decodes it as UTF-8. A document not written yet is
    "none yet": a 404, or before COSMOS 7.5 a 500 saying the file is not there."""
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
    if (url.includes('gone')) return {{ ok: false, status: 500, json: async () => ({{ status: 'error', message: 'No such file or directory @ rb_sysopen - /tmp/d1/DEFAULT/constellation/gone.json' }}) }};
    if (url.includes('broken')) return {{ ok: false, status: 500, json: async () => ({{ status: 'error', message: 'Redis down' }}) }};
    return {{ ok: true, json: async () => ({{ contents: {json.dumps(contents)} }}) }};
  }},
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(TOOL))}, 'utf8'), sandbox);
(async () => {{
  const doc = await exported.load('index.json');
  const failed = async (name) => {{ try {{ await exported.load(name); }} catch (e) {{ return e.message; }} }};
  const missing = await failed('missing.json'), gone = await failed('gone.json'), broken = await failed('broken.json');
  const kinds = ['bootstrap', 'mount', 'unmount'].map((k) => typeof exported[k]);
  console.log(JSON.stringify({{ kinds, doc, missing, gone, broken, calls }}));
}})();
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    assert got["kinds"] == ["function"] * 3
    assert got["doc"] == doc and got["missing"] == got["gone"] == "none yet"
    assert got["broken"] == "COSMOS answered 500"
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


def test_the_modcod_plan_is_the_next_aos_like_the_next_pass_and_none_with_nothing_ahead(bridge):
    module, _ = bridge
    under_way = plan(-0.05, ("QPSK 1/2", 1.0), ("8PSK 2/3", 7.0))
    ahead = plan(2, ("QPSK 3/4", 4.0), ("8PSK 3/4", 8.0))
    modcod = module.modcod_packet([under_way, ahead], NOW)
    assert modcod["AOS"] == module.packets([under_way, ahead], [], NOW)["NEXT_PASS"]["AOS"]
    assert modcod["AOS"] == module._minute(ahead["target_at"]) and modcod["FIRST"] == "QPSK 3/4"
    assert module.modcod_packet([under_way], NOW) is None


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
    # When the run arrived, as COSMOS's own received time of RUN moves with every refresh.
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\dZ", sent["RUN"]["RECEIVED"])


def test_a_kind_no_run_has_brought_is_not_injected_as_a_forecast_of_nothing(bridge):
    """COSMOS shows no value for a packet no run of its kind has filled, never NONE and 0, which
    read as a forecast of nothing; a run of the kind with nothing in it does say NONE."""
    module, injected = bridge
    forecast = module.Forecast()
    forecast.take(_resource("rain-fade-swap"), [swap(3, 10, 60)])
    forecast.publish(NOW)
    # A swap run brings HELD too: what Constellation holds for a decision.
    assert [p for p, _ in injected] == ["SWAP", "HELD", "RUN"]
    injected.clear()
    forecast.take(_resource("pass-yield"), [])
    forecast.publish(NOW)
    sent = dict(injected)
    assert sent["NEXT_PASS"] == module.NO_PASS and sent["NEXT_24H"]["PASSES"] == 0
    assert set(sent) == {"NEXT_PASS", "NEXT_24H", "SWAP", "HELD", "RUN"}
    # Kept and restored as not arrived.
    restored = module.Forecast()
    restored.restore(json.loads(json.dumps(forecast.saved())))
    assert restored.capacity is None and restored.plans is None


def test_the_run_packet_says_it_is_the_latest_run_not_the_source_of_every_value():
    target = ROOT / "bridges/cosmos/targets/CONSTELLATION"
    tlm = (target / "cmd_tlm/tlm.txt").read_text()
    assert 'TELEMETRY CONSTELLATION RUN BIG_ENDIAN "The latest workflow run received' in tlm
    assert 'VERTICALBOX "Latest run received"' in (target / "screens/forecast.txt").read_text()


def tlm_items() -> list[tuple[str, str, int, str]]:
    """Every item of tlm.txt: (packet, item, bits, type)."""
    rows, packet = [], ""
    for line in (ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt").read_text().splitlines():
        words = line.split()
        if words[:1] == ["TELEMETRY"]:
            # NEXT_PASS is written once, as the template NEXT_PASS_<NAME> repeats.
            packet = "NEXT_PASS" if words[2] == "<%=" else words[2]
        elif words[:1] == ["APPEND_ITEM"]:
            rows.append((packet, words[1], int(words[2]), words[3]))
    return rows


def test_a_value_longer_than_its_string_item_is_cut_to_fit_not_dropped_by_cosmos(bridge):
    """COSMOS drops a whole injected packet with a value longer than its STRING item, and the
    API takes ids longer than the items hold, so the bridge cuts each to its item's bytes."""
    module, injected = bridge
    sizes = {(name, bits // 8) for _, name, bits, kind in tlm_items() if kind == "STRING"}
    assert set(module.STRING_BYTES.items()) == sizes
    long = item(2, 10)
    long["entity"] = {**long["entity"], "satellite_id": "é" * 40, "antenna_id": "a" * 128}
    forecast = module.Forecast()
    forecast.take({**_resource("pass-yield"), "constellation.run_id": "r" * 200}, [long])
    forecast.publish(NOW)
    sent = dict(injected)
    assert sent["NEXT_PASS"]["SATELLITE"] == "é" * 32 and sent["NEXT_PASS"]["ANTENNA"] == "a" * 64
    assert sent["RUN"]["RUN_ID"] == "r" * 64


def test_summed_gb_items_are_64_bit_so_a_large_sum_shows_its_third_decimal():
    """A 32-bit float of 50000.123 reads 50000.121 at %.3f: a day's GB summed across a network
    passes that, a single pass's never does."""
    bits = {(p, i): (b, t) for p, i, b, t in tlm_items()}
    for summed in [
        ("NEXT_24H", "EXPECTED_GB_P10"),
        ("NEXT_24H", "EXPECTED_GB_P50"),
        ("CAPACITY", "GB_P10"),
        ("CAPACITY", "GB_P50"),
    ]:
        assert bits[summed] == (64, "FLOAT"), summed


def test_minutes_to_deadline_says_where_its_inclusive_limits_turn_yellow_and_red():
    """COSMOS limits are inclusive: a value at red low is red, so 15.0 minutes left is red."""
    lines = (ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt").read_text().splitlines()
    at = next(n for n, x in enumerate(lines) if "APPEND_ITEM MINUTES_TO_DEADLINE" in x)
    limits = next(x.split() for x in lines[at:] if x.split()[:1] == ["LIMITS"])
    red, yellow = limits[4], limits[5]
    assert (red, yellow) == ("15", "30")
    assert f"yellow at {yellow} or less, red at {red} or less" in lines[at]


def test_min_sinr_says_it_is_the_lowest_at_a_modcod_change():
    """The API's MODCOD series keeps only the steps where the MODCOD changes."""
    tlm = (ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt").read_text()
    (line,) = [x for x in tlm.splitlines() if "APPEND_ITEM MIN_SINR_DB" in x]
    assert "at a MODCOD change" in line and "over the pass" not in line


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


def network(monkeypatch, module, answer) -> None:
    """Every HTTP call answered by `answer(req, timeout)`: a keyed one too, which goes through an
    opener that follows no redirect."""
    monkeypatch.setattr(module.urllib.request, "urlopen", answer)
    opener = types.SimpleNamespace(open=answer)
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *handlers: opener)


def polled(
    monkeypatch,
    bridge,
    sleeps: int,
    rotate=None,
    refused: dict[str, int] | None = None,
    answer: bytes = b"{}",
    poll_s: float = 10**6,
) -> tuple[list, list]:
    """Runs the bridge's loop through `sleeps` sleeps against a feed with nothing new. Returns the
    key each request carried (path, key) and the sleeps. `rotate(n)` runs at the
    n-th sleep; a key in `refused` gets its HTTP status, with `answer` as the body. A wait is one
    sleep unless `poll_s` is set, the most the bridge sleeps between refreshes."""
    module, _ = bridge
    monkeypatch.setattr(module, "POLL_S", poll_s)
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

    network(monkeypatch, module, urlopen)
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


def test_in_cosmos_the_log_goes_to_its_log_messages_not_the_operators_held_back_stdout(
    bridge, monkeypatch, capsys
):
    module, _ = bridge
    logged = []

    class Logger:
        microservice_name = None
        info = staticmethod(lambda line, scope: logged.append(("INFO", line, scope)))
        warn = staticmethod(lambda line, scope: logged.append(("WARN", line, scope)))

    monkeypatch.setitem(
        sys.modules, "openc3.utilities.logger", types.SimpleNamespace(Logger=Logger)
    )
    monkeypatch.setenv("OPENC3_MICROSERVICE_NAME", "DEFAULT__USER__CONSTELLATION-BRIDGE")
    module.log("first start")
    module.log("no key", warn=True)
    assert logged == [
        ("INFO", "constellation-bridge: first start", "DEFAULT"),
        ("WARN", "constellation-bridge: no key", "DEFAULT"),
    ]
    assert Logger.microservice_name == "DEFAULT__USER__CONSTELLATION-BRIDGE"
    assert capsys.readouterr() == ("", "")


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
    assert "predictions:read there (https://platform.test/keys)" in out.err
    # Nothing in the feed for this connection yet: the log says what to do in Constellation.
    assert "no run for cosmos-bridge yet" in out.out and "connection cosmos-bridge" in out.out
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


@pytest.mark.parametrize(
    "status, answer",
    [
        (401, {}),
        (402, {"detail": {"error": "upgrade_required"}}),
        (429, {"error": "rate_limited", "retry_after_seconds": 30}),
    ],
)
def test_while_a_key_or_plan_holds_the_bridge_off_the_forecast_held_keeps_refreshing(
    bridge, monkeypatch, secrets, status, answer
):
    """The minutes left to a swap and the next pass move with the clock: a refused key or a hold
    of an hour never freezes them."""
    module, injected = bridge
    monkeypatch.setattr(
        module, "resume", lambda _, f, d=None: setattr(f, "run", {"WORKFLOW": "w"}) or ""
    )
    monkeypatch.setattr(module, "REFRESH_S", -1)
    secrets.key = "good"
    _, slept = polled(
        monkeypatch,
        bridge,
        9,
        rotate=lambda n: n == 1 and setattr(Secrets, "key", "held"),
        refused={"held": status},
        answer=json.dumps(answer).encode(),
        poll_s=15,
    )
    assert max(slept) <= 15
    assert len([p for p, _ in injected if p == "RUN"]) == len(slept)


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

    network(
        monkeypatch,
        module,
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

    network(
        monkeypatch,
        module,
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
    assert out["SWAP"]["ADVICE"] == "No backup pass in the window"


def test_the_swap_packet_reads_as_advice_with_the_backups_los_gb_and_the_gain(bridge):
    module, _ = bridge
    s = swap(1, 10, 20)
    to_at, to_until = (NOW + timedelta(hours=3)).isoformat(), (NOW + timedelta(hours=3, minutes=6)).isoformat()
    action = {**s["action"], "to_at": to_at, "to_until": to_until, "to_gb": 4.0, "gain_gb": 1.5, "action": "move", "reason": "overlap"}
    out = module.packets([], [{**s, "action": action}], NOW)["SWAP"]
    assert (out["BACKUP_AOS"], out["BACKUP_LOS"]) == ("2026-11-06 15:00Z", "2026-11-06 15:06Z")
    assert (out["BACKUP_GB"], out["GAIN_GB"]) == (4.0, 1.5)
    assert out["ADVICE"].startswith("Move to the backup on ant-2 at 2026-11-06 15:00Z to 15:06Z (4.0 GB)")
    assert "both passes use one radio at once" in out["ADVICE"] and "cancel" not in out["ADVICE"].lower()
    # An API from before the backup's LOS and GB: empty and 0, never a guess.
    old = module.packets([], [s], NOW)["SWAP"]
    assert (old["BACKUP_LOS"], old["BACKUP_GB"], old["GAIN_GB"]) == ("", 0.0, 0.0)


def test_the_swap_screen_says_decide_by_and_never_shows_the_p10_as_if_rain(bridge):
    screen = (ROOT / "bridges/cosmos/targets/CONSTELLATION/screens/forecast.txt").read_text()
    tlm = (ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt").read_text()
    assert "Cancel" not in screen and "cancel" not in tlm.lower()
    assert "SWAP KEEPS_GB_P10" not in screen and "GB if dry" not in screen
    for name in ("ADVICE", "BACKUP_LOS", "BACKUP_GB", "GAIN_GB"):
        assert f"SWAP {name} " in screen


def test_the_screen_shows_one_expected_gb_never_a_p10_or_p90():
    screen = (ROOT / "bridges/cosmos/targets/CONSTELLATION/screens/forecast.txt").read_text()
    assert "P10" not in screen and "P90" not in screen
    assert "p10" not in screen.lower() and "p90" not in screen.lower()
    assert screen.count('"GB expected"') >= 3


def test_a_held_swap_run_counts_its_held_items_and_never_takes_them_for_passes(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    forecast.take(_resource("pass-yield"), [item(2, 10)])
    forecast.take(_resource("rain-fade-swap"), [held(3, 10), held(4, 10), approved(5, 10, 60)])
    assert len(forecast.held) == 2 and [s["entity"]["id"] for s in forecast.swaps] == ["p5"]
    assert [p["entity"]["id"] for p in forecast.passes] == ["p2"]
    assert module.priced_passes([held(3, 10)]) == []
    # A state saved before held swaps were counted restores with none held.
    restored = module.Forecast()
    restored.restore({k: v for k, v in forecast.saved().items() if k != "held"})
    assert restored.held == []


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
    # Items added later go after the rest, so the packet's earlier items keep their place.
    assert items[-7:] == ["STATUS", "APPROVED", "PENDING", "ADVICE", "BACKUP_LOS", "BACKUP_GB", "GAIN_GB"]


def test_a_held_action_sent_with_its_body_is_counted_held_not_shown(bridge):
    module, _ = bridge
    with_body = {**swap(3, 10, 60), "action_status": "pending"}
    assert module.swap_items([with_body]) == []
    forecast = module.Forecast()
    forecast.take(_resource("rain-fade-swap"), [with_body])
    assert forecast.swaps == [] and len(forecast.held) == 1


def test_a_held_swap_past_its_deadline_is_no_longer_counted(bridge):
    module, injected = bridge
    forecast = module.Forecast()
    late = {**swap(3, 10, 30), "action_status": "pending"}
    forecast.take(_resource("rain-fade-swap"), [late, held(4, 10)])
    forecast.publish(NOW)
    forecast.publish(NOW + timedelta(minutes=31))
    assert [v["PENDING"] for p, v in injected if p == "SWAP"] == [2, 1]


def test_a_held_swap_as_the_api_sends_it_counts_until_the_api_would_expire_it(bridge):
    """The API sends a held item without its action, so no deadline: it expires as the API's
    expires_at() says, when its pass starts, or a link's span ends."""
    module, injected = bridge
    link = {
        **held(1, 10),
        "entity": {"type": "link", "id": "l1"},
        "until": (NOW + timedelta(hours=5)).isoformat(),
    }
    forecast = module.Forecast()
    forecast.take(_resource("rain-fade-swap"), [held(1, 10), link])
    for hours in (0, 2, 6):
        forecast.publish(NOW + timedelta(hours=hours))
    assert [v["PENDING"] for p, v in injected if p == "SWAP"] == [2, 1, 0]


def test_a_state_kept_by_0_8_0_restores_its_held_count(bridge):
    module, injected = bridge
    forecast = module.Forecast()
    forecast.restore({"passes": [], "swaps": [], "pending": 3, "run": {"WORKFLOW": "w"}})
    forecast.publish(NOW)
    assert [v["PENDING"] for p, v in injected if p == "SWAP"] == [3]


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
    monkeypatch.setattr(module, "resume", lambda consumer, forecast, delivered=None: "")
    monkeypatch.setattr(module, "keep", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    (report,) = reports
    assert report["acted"] == 1


def test_a_run_reports_each_approvals_outcome_acted_or_refused_past_its_deadline(
    bridge, monkeypatch
):
    shown = {**approved(2, 10, 30), "action_id": "a1", "decision_id": "d1"}
    late = {**approved(4, 10, -5), "action_id": "a2", "decision_id": "d2"}
    feed = fed((1, [shown, late]))
    module = load(monkeypatch)
    monkeypatch.setattr(module.results, "now", lambda: NOW)
    reports = []

    def call(method, path, body=None):
        if path.startswith("/predictions/feed?"):
            after = dict(urllib.parse.parse_qsl(path.split("?", 1)[1])).get("after", "")
            entries = [e for e in feed if e["cursor"] > after][:1]
            return {"entries": entries, "next": entries[-1]["cursor"] if entries else after}
        if path == "/predictions/feed/deliveries":
            reports.append(body)
        return None

    sleeps = iter(range(1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "inject", lambda packet, values: None)
    monkeypatch.setattr(module, "publish_documents", lambda *a: None)
    monkeypatch.setattr(module, "resume", lambda consumer, forecast, delivered=None: "")
    monkeypatch.setattr(module, "keep", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    (report,) = reports
    assert [(a["action_id"], a["outcome"]) for a in report["actions"]] == [
        ("a1", "acted"),
        ("a2", "refused"),
    ]
    assert report["actions"][0]["reason"] == "shown in the CONSTELLATION SWAP packet"


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
    monkeypatch.setattr(module, "resume", lambda consumer, forecast, delivered=None: "")
    monkeypatch.setattr(
        module, "keep", lambda c, cur, forecast, delivered=None: kept.append(forecast.saved())
    )
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    assert [r.get("acted") for r in reports] == [1, None]
    restarted = module.Forecast()
    restarted.restore(kept[-1])
    assert module.results.first_acted(module.swap_items([approval]), restarted.acted) == 0


def started(module, monkeypatch) -> list[float] | None:
    """Runs the bridge's start: the sleeps it waits on before reaching the poll loop (after 3, it
    is taken to wait for good), or None when it reaches it."""
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise StopIteration

    monkeypatch.setattr(module, "api_key", lambda: (_ for _ in ()).throw(SystemExit("started")))
    monkeypatch.setattr(module.results, "http", lambda *a, **k: pytest.fail("sent"))
    monkeypatch.setattr(module.time, "sleep", sleep)
    try:
        module.main()
    except StopIteration:
        return slept
    except SystemExit as stopped:
        assert stopped.code == "started"
    return None


@pytest.mark.parametrize(
    "url, waits",
    [("http://example.com", True), ("http://127.0.0.1:8000", False), ("https://api.test", False)],
)
def test_plain_http_to_another_host_is_said_once_and_waits_before_the_key_is_sent(
    bridge, monkeypatch, capsys, url, waits
):
    """Said once and waited on, not exited: COSMOS's operator respawns a microservice that exits,
    which would say it again every cycle."""
    module, _ = bridge
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", url)
    slept = started(module, monkeypatch)
    if waits:
        assert slept == [module.KEY_WAIT_S] * 3
        (line,) = capsys.readouterr().err.splitlines()
        assert "not sending the API key to http://example.com" in line and "https" in line
    else:
        assert slept is None


@pytest.mark.parametrize(
    "consumer, waits",
    [
        ("Ops COSMOS", True),
        ("cosmos_lab", True),
        ("COSMOS-2", True),
        ("-cosmos", True),
        ("a" * 64, True),
        ("cosmos-2", False),
        ("a" * 63, False),
    ],
)
def test_a_consumer_constellation_would_refuse_is_said_once_and_waits(
    bridge, monkeypatch, capsys, consumer, waits
):
    """The API takes a connection name of lowercase letters, digits and hyphens, at most 63, not
    starting with a hyphen; any other is a bare 422 on every call."""
    module, _ = bridge
    monkeypatch.setenv("CONSUMER", consumer)
    slept = started(module, monkeypatch)
    if waits:
        assert slept == [module.KEY_WAIT_S] * 3
        (line,) = capsys.readouterr().err.splitlines()
        assert repr(consumer) in line and "lowercase letters, digits and hyphens" in line
    else:
        assert slept is None


def test_the_screen_shows_every_packet_and_only_items_the_packets_have():
    target = ROOT / "bridges/cosmos/targets/CONSTELLATION"
    items, packet = set(), None
    for line in (target / "cmd_tlm/tlm.txt").read_text().splitlines():
        words = line.split()
        if words[:1] == ["TELEMETRY"]:
            # NEXT_PASS is written once, as the template NEXT_PASS_<NAME> repeats.
            packet = "NEXT_PASS" if words[2] == "<%=" else words[2]
        elif words[:1] == ["APPEND_ITEM"]:
            items.add((packet, words[1]))
    widgets = ("LABELVALUE", "VALUE", "TEXTBOX")
    shown = {
        (words[2], words[3])
        for line in (target / "screens/forecast.txt").read_text().splitlines()
        if (words := line.split()) and words[0].startswith(widgets) and "<%" not in line
    }
    assert shown <= items
    assert {p for p, _ in shown} == {p for p, _ in items}


@pytest.mark.parametrize(
    "packet, empty",
    [
        ("NEXT_PASS", "NO_PASS"),
        ("CAPACITY", "NO_CAPACITY"),
        ("MODCOD", "NO_MODCOD"),
        ("HELD", "NO_HELD"),
    ],
)
def test_each_empty_packet_writes_every_item_of_its_packet(bridge, packet, empty):
    module, _ = bridge
    assert list(getattr(module, empty)) == [name for p, name, _, _ in tlm_items() if p == packet]


def test_an_api_outage_is_said_once_and_the_forecast_held_keeps_refreshing(
    bridge, monkeypatch, capsys
):
    module, injected = bridge
    # A forecast is held from before.
    monkeypatch.setattr(
        module, "resume", lambda _, f, d=None: setattr(f, "run", {"WORKFLOW": "w"}) or ""
    )
    monkeypatch.setattr(module, "REFRESH_S", -1)
    down = {"n": 0}

    def urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        if down["n"] == 2:
            raise urllib.error.URLError("Connection refused")
        if path == "/predictions/feed" and 1 <= down["n"] <= 3:
            raise urllib.error.HTTPError(req.full_url, 503, "", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return io.BytesIO(b'{"entries": [], "next": ""}')

    def sleep(seconds):
        down["n"] += 1
        if down["n"] == 6:
            raise StopIteration

    network(monkeypatch, module, urlopen)
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        module.main()
    out = capsys.readouterr()
    assert out.out.count("reached Constellation again") == 1
    assert out.err.splitlines() == [
        "constellation-bridge: Constellation answered HTTP 503. Showing the forecast held and"
        " asking again every 15 s",
        "constellation-bridge: could not reach Constellation (Connection refused). Showing the"
        " forecast held and asking again every 15 s",
        "constellation-bridge: Constellation answered HTTP 503. Showing the forecast held and"
        " asking again every 15 s",
    ]
    assert len([p for p, _ in injected if p == "RUN"]) == 6


def test_a_state_kept_for_another_api_is_not_resumed(bridge, monkeypatch, capsys):
    stored: dict[str, bytes] = {}
    ran(monkeypatch, fed((1, [item(2, 10)])), stored)
    # The same connection, repointed at another API: its cursor means nothing there.
    monkeypatch.setitem(os.environ, "CONSTELLATION_API_URL", "https://api.other.test")
    again = fed((1, [item(2, 10)]), (2, [item(3, 20)]), (3, [item(4, 30)]))
    _, reported, afters = ran(monkeypatch, again, stored)
    # A first start's: the newest run only, not runs 2 and 3 after the kept cursor.
    assert afters[0] == cursor_of(2) and reported == ["run_3"]
    assert "state kept for https://api.test is not for this API" in capsys.readouterr().out
    # A state kept before the API was (0.8.0) is resumed.
    (key,) = stored
    kept = json.loads(stored[key])
    stored[key] = json.dumps({k: v for k, v in kept.items() if k != "api"}).encode()
    _, _, afters = ran(monkeypatch, again, stored)
    assert afters[0] == cursor_of(3)


def test_a_first_start_whose_feed_has_one_run_never_says_there_is_none(
    bridge, monkeypatch, capsys
):
    stored: dict[str, bytes] = {}
    _, reported, _ = ran(monkeypatch, fed((1, [item(2, 10)])), stored)
    assert reported == ["run_1"] and "no run for" not in capsys.readouterr().out


# Per-satellite packets, the contract minimum and HELD.

TLM = ROOT / "bridges/cosmos/targets/CONSTELLATION/cmd_tlm/tlm.txt"


def packet_items(text: str, header: str) -> list[str]:
    """The APPEND_ITEM names of the packet whose TELEMETRY line starts with `header`."""
    block = text.split(header, 1)[1].split("\nTELEMETRY ", 1)[0].split("<%- end", 1)[0]
    return [line.split()[1] for line in block.splitlines() if line.strip().startswith("APPEND_ITEM")]


def render(path: Path, **variables: str) -> str:
    """`path` as COSMOS renders a plugin file: ERB, trim mode "-", the plugin's variables."""
    script = (
        "b = binding; ARGV.each_slice(2) { |k, v| b.local_variable_set(k.to_sym, v) };"
        " print ERB.new(File.read(ENV['FILE']), trim_mode: '-').result(b)"
    )
    args = [x for kv in variables.items() for x in kv]
    out = subprocess.run(
        ["ruby", "-rerb", "-e", script, *args],
        env={**os.environ, "FILE": str(path)},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_every_pass_value_is_an_item_of_the_next_pass_packets(bridge):
    module, _ = bridge
    text = TLM.read_text()
    items = packet_items(text, "TELEMETRY CONSTELLATION <%= packet %> ")
    assert list(module.pass_values(item(2, 10), None)) == items
    assert list(module.NO_PASS) == items
    assert list(module.NO_HELD) == packet_items(text, "TELEMETRY CONSTELLATION HELD ")
    out = module.packets([item(2, 10)], [], NOW)
    assert list(out["NEXT_24H"]) == packet_items(text, "TELEMETRY CONSTELLATION NEXT_24H ")


@pytest.mark.skipif(shutil.which("ruby") is None, reason="needs ruby to render the ERB")
def test_tlm_has_a_next_pass_packet_per_mapped_satellite_named_as_the_bridge_names_it(bridge):
    module, _ = bridge
    rendered = render(TLM, satellites="sat-1=kestrel-1 sat-2=K2 broken")
    names = [line.split()[2] for line in rendered.splitlines() if line.startswith("TELEMETRY ")]
    own = [f"NEXT_PASS_{module.packet_name(n)}" for n in ("kestrel-1", "K2")]
    assert names[:3] == ["NEXT_PASS", *own]
    assert "<%" not in rendered
    assert render(TLM, satellites="").count("TELEMETRY CONSTELLATION NEXT_PASS") == 1


def test_at_risk_is_written_as_a_state_the_packet_names(bridge):
    module, _ = bridge
    states = [line.split()[1] for line in TLM.read_text().splitlines() if line.strip().startswith("STATE ")]
    assert {"NO", "YES"} <= set(states)
    assert module.pass_values(item(2, 3), None)["AT_RISK"] in states
    assert module.NO_PASS["AT_RISK"] in states


def test_a_pass_is_at_risk_as_the_platforms_passes_tab_says(bridge):
    """planView isAtRisk: expected (p50) to deliver nothing or under the contract minimum, a value
    its pricing left unfilled, or flagged by a workflow."""
    module, _ = bridge
    passes = [item(2, 5), item(5, 10), item(8, 4)]
    out = module.packets(passes, [], NOW, minimum=6.0)
    assert out["NEXT_PASS"]["AT_RISK"] == "YES" and out["NEXT_PASS"]["CONTRACT_MIN_GB"] == 6.0
    assert out["NEXT_24H"]["AT_RISK"] == 2
    # With no minimum set, none of these is at risk: each is expected to deliver something.
    out = module.packets(passes, [], NOW)
    assert out["NEXT_PASS"]["AT_RISK"] == "NO" and out["NEXT_24H"]["AT_RISK"] == 0
    assert out["NEXT_PASS"]["CONTRACT_MIN_GB"] == 0.0
    nothing = item(2, 0)
    unfilled = item(2, 10)
    unfilled["explain"] = {**unfilled["explain"], "weather": {"value": None, "source": "unavailable:x"}}
    flagged = item(2, 10, severity="warning")
    for risky in (nothing, unfilled, flagged):
        assert module.at_risk(risky, None), risky
    assert not module.at_risk(item(2, 10), 9.9)

def test_each_mapped_satellite_gets_its_own_next_pass_and_none_says_so(bridge):
    module, _ = bridge
    sat2 = item(1, 10)
    sat2["entity"] = {**sat2["entity"], "satellite_id": "sat-2", "id": "p_sat2"}
    out = module.packets(
        [item(3, 10), sat2], [], NOW, satellites={"sat-1": "kestrel-1", "sat-9": "K9"}
    )
    assert out["NEXT_PASS"]["SATELLITE"] == "sat-2"
    assert out["NEXT_PASS_KESTREL_1"]["SATELLITE"] == "sat-1"
    assert out["NEXT_PASS_KESTREL_1"]["AOS"] == "2026-11-06 15:00Z"
    assert out["NEXT_PASS_K9"] == {**module.NO_PASS, "SATELLITE": "sat-9"}


def test_satellites_and_outcomes_variables_read_as_pairs(bridge, capsys):
    module, _ = bridge
    assert module.pairs("sat-1=KESTREL1  sat-2=K2") == {"sat-1": "KESTREL1", "sat-2": "K2"}
    assert module.pairs("sat-1 =x") == {}
    assert "left out" in capsys.readouterr().err


def action(minutes: int, status: str = "pending", item_id: str = "pass_1") -> dict:
    """A held action as GET /predictions/actions lists it."""
    deadline = (NOW + timedelta(minutes=minutes)).isoformat()
    return {
        "run_id": "run_rfs",
        "item_id": item_id,
        "entity": {"type": "pass", "id": item_id, "satellite_id": "sat-1", "antenna_id": "ant-1"},
        "target_at": (NOW + timedelta(minutes=minutes + 15)).isoformat(),
        "action": {
            "verb": "swap",
            "mode": "approve",
            "to": "pass_b",
            "to_antenna_id": "ant-2",
            "gain_gb": 1.5,
            "deadline": deadline,
        },
        "expires_at": deadline,
        "status": status,
        "workflow": "rain-fade-swap",
    }


def test_held_shows_the_soonest_swap_waiting_on_a_decision_with_its_ids(bridge):
    module, _ = bridge
    held = [
        action(90, item_id="later"),
        action(40, item_id="soonest"),
        action(-5, item_id="expired"),
        action(10, status="approved", item_id="decided"),
    ]
    out = module.packets([], [], NOW, waiting=held)
    assert out["HELD"]["OPEN"] == 2
    assert (out["HELD"]["RUN_ID"], out["HELD"]["ITEM_ID"]) == ("run_rfs", "soonest")
    assert out["HELD"]["MINUTES_TO_DEADLINE"] == 40.0 and out["HELD"]["BACKUP_ANTENNA"] == "ant-2"
    assert module.packets([], [], NOW, waiting=[])["HELD"] == module.NO_HELD
    # No swap workflow delivers here: no HELD packet at all.
    assert "HELD" not in module.packets([], [], NOW)


def http_error(status: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.test", status, "", {}, io.BytesIO(json.dumps(body).encode())
    )  # type: ignore[arg-type]


def test_held_is_read_per_swap_workflow_and_a_failed_read_keeps_the_last(bridge, monkeypatch):
    module, _ = bridge
    forecast = module.Forecast()
    forecast.take(_resource("rain-fade-swap"), [held(3, 10)])
    asked, answers = [], iter([{"actions": [action(30)]}, http_error(500, {})])

    def call(method, path, body=None):
        asked.append(path)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(module, "call", call)
    said: set[str] = set()
    module.read_waiting(forecast, said)
    assert asked == ["/predictions/actions?workflow=w"]
    assert [a["item_id"] for a in forecast.waiting] == ["pass_1"]
    module.read_waiting(forecast, said)
    assert [a["item_id"] for a in forecast.waiting] == ["pass_1"] and len(said) == 1


def test_the_contract_minimum_is_read_and_unset_reads_as_none(bridge, monkeypatch):
    module, _ = bridge
    forecast = module.Forecast()
    answers = iter([{"min_gb": 2.5}, {"min_gb": None}, http_error(403, {"error": "x"}), http_error(404, {})])

    def call(method, path, body=None):
        assert path == "/predictions/contract-minimum"
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(module, "call", call)
    said: set[str] = set()
    module.read_minimum(forecast, said)
    assert forecast.minimum == 2.5
    module.read_minimum(forecast, said)
    assert forecast.minimum is None
    forecast.minimum = 1.0
    module.read_minimum(forecast, said)  # refused: the last one stands
    assert forecast.minimum == 1.0 and len(said) == 1
    module.read_minimum(forecast, said)  # not served here
    assert forecast.minimum is None


# Outcomes: what each pass delivered, from the interface's received bytes.


def watched(module, *passes: dict) -> object:
    outcomes = module.Delivered({"sat-1": "SAT1_INT"})
    outcomes.follow(list(passes), NOW - timedelta(hours=1))
    return outcomes


def reading(outcomes, at: datetime, count: int) -> None:
    outcomes.read(at.timestamp(), lambda name: count)


def test_a_pass_that_brought_bytes_is_sent_as_a_contact_record(bridge):
    module, _ = bridge
    p = item(-1, 10)  # AOS an hour ago, LOS 8 minutes later
    aos, los = NOW - timedelta(hours=1), NOW - timedelta(minutes=52)
    outcomes = watched(module, p)
    reading(outcomes, aos - timedelta(seconds=10), 1000)
    reading(outcomes, los + timedelta(seconds=5), 4000)
    records, dropped = outcomes.due(NOW)
    assert dropped == {}
    ((pid, record),) = records
    assert pid == "p-1"
    assert record == {
        "time": p["target_at"],
        "ground_station_id": "gs",
        "ground_antenna_id": "ant-1",
        "satellite_id": "sat-1",
        "contact_id": "p-1",
        "fields": {"bytes_delivered": 3000, "contact_status": "ok", "los_utc": p["until"]},
    }
    outcomes.mark_sent([pid], NOW.timestamp())
    outcomes.follow([p], NOW)
    assert outcomes.due(NOW) == ([], {})


def test_a_pass_it_cannot_measure_is_never_sent(bridge):
    module, _ = bridge
    aos, los = NOW - timedelta(hours=1), NOW - timedelta(minutes=52)
    quiet = watched(module, item(-1, 10))
    reading(quiet, aos - timedelta(seconds=10), 1000)
    reading(quiet, los + timedelta(seconds=5), 1000)
    assert "no bytes arrived" in quiet.due(NOW)[1]["p-1"]

    unseen = watched(module, item(-1, 10))
    reading(unseen, los + timedelta(seconds=5), 9000)
    assert "not reading" in unseen.due(NOW)[1]["p-1"]

    reset = watched(module, item(-1, 10))
    reading(reset, aos - timedelta(seconds=10), 9000)
    reading(reset, los + timedelta(seconds=5), 10)
    assert "went back" in reset.due(NOW)[1]["p-1"]

    other = item(-0.95, 10)
    other["entity"] = {**other["entity"], "id": "p_other", "antenna_id": "ant-2"}
    both = watched(module, item(-1, 10), other)
    reading(both, aos - timedelta(seconds=10), 1)
    reading(both, NOW, 5)
    records, dropped = both.due(NOW)
    assert records == [] and set(dropped) == {"p-1", "p_other"}

    # A pass still flying, or just ended, waits.
    soon = watched(module, item(-0.1, 10))
    assert soon.due(NOW) == ([], {})


def test_a_pass_forecast_again_replaces_the_one_watched_rather_than_overlap_it(bridge):
    """A newer run can forecast the same pass under a new id or a moved AOS; the old one gives
    way, or the two would read as two passes at once and neither be sent."""
    module, _ = bridge
    first = item(-1, 10)
    again = item(-0.98, 10)  # about a minute later, a new id
    outcomes = module.Delivered({"sat-1": "SAT1_INT"})
    outcomes.follow([first], NOW - timedelta(hours=2))
    outcomes.follow([again], NOW - timedelta(hours=2))
    assert list(outcomes.watch) == [again["entity"]["id"]]
    aos = datetime.fromisoformat(again["target_at"])
    los = datetime.fromisoformat(again["until"])
    reading(outcomes, aos - timedelta(seconds=10), 100)
    reading(outcomes, los + timedelta(seconds=5), 900)
    ((pid, record),) = outcomes.due(NOW)[0]
    assert pid == again["entity"]["id"] and record["fields"]["bytes_delivered"] == 800
    # One that ended and is not in the newer run, with nothing overlapping it, is still measured.
    other = item(-3, 10)
    outcomes.follow([other], NOW - timedelta(hours=4))
    outcomes.follow([item(5, 10)], NOW)
    assert other["entity"]["id"] in outcomes.watch


def test_outcomes_are_posted_and_wait_on_a_key_refused_telemetry_write(bridge, monkeypatch):
    module, _ = bridge
    aos = NOW - timedelta(hours=1)
    forecast = module.Forecast()
    forecast.passes = [item(-1, 10)]
    outcomes = module.Delivered({"sat-1": "SAT1_INT"})
    outcomes.readings["SAT1_INT"] = [[(aos - timedelta(seconds=10)).timestamp(), 0]]
    monkeypatch.setattr(module, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: NOW)}))
    monkeypatch.setattr(module, "rxbytes", lambda name: 5_000_000)
    posts = []
    answers = iter([http_error(403, {"error": "insufficient_scope", "required": "telemetry:write"}), {"accepted_count": 1}])

    def call(method, path, body=None):
        posts.append((method, path, body))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(module, "call", call)
    said: set[str] = set()
    module.measure_delivered(outcomes, forecast, "read-only", said)
    assert outcomes.refused == "read-only" and any("telemetry:write" in s for s in said)
    module.measure_delivered(outcomes, forecast, "read-only", said)
    assert len(posts) == 1  # the refused key is not asked again
    module.measure_delivered(outcomes, forecast, "with-telemetry-write", said)
    assert len(posts) == 2 and posts[1][:2] == ("POST", "/telemetry")
    assert posts[1][2][0]["fields"]["bytes_delivered"] == 5_000_000
    assert "p-1" in outcomes.sent and outcomes.watch == {}


# Timeline: reserve activities for the forecast passes.


def calendar(monkeypatch, have: list[dict], enterprise: bool = True) -> list[tuple]:
    """openc3.script's calendar functions over `have`, recording each write, on a COSMOS that is
    Enterprise or Core as its /openc3-api/info says."""
    calls: list[tuple] = []
    script = types.ModuleType("openc3.script")
    info = types.SimpleNamespace(status_code=200, json=lambda: {"enterprise": enterprise})
    script.API_SERVER = types.SimpleNamespace(  # type: ignore[attr-defined]
        request=lambda method, path: calls.append(("info",)) or info
    )
    script.list_timelines = lambda: [{"name": "OTHER"}]  # type: ignore[attr-defined]
    script.create_timeline = lambda name, color=None: calls.append(("timeline", name))  # type: ignore[attr-defined]
    script.get_timeline_activities = lambda name, start=None, stop=None: have  # type: ignore[attr-defined]
    script.delete_timeline_activity = lambda name, start, uuid: calls.append(("delete", uuid))  # type: ignore[attr-defined]
    script.create_timeline_activity = lambda name, kind, start, stop, data: calls.append(  # type: ignore[attr-defined]
        ("create", kind, data["pass_id"], int(start.timestamp()))
    )
    package = types.ModuleType("openc3")
    package.script = script  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openc3", package)
    monkeypatch.setitem(sys.modules, "openc3.script", script)
    return calls


def test_the_timeline_holds_the_forecast_passes_and_moves_only_its_own(bridge, monkeypatch):
    module, _ = bridge
    kept, moved, new = item(2, 10), item(5, 10), item(9, 10)
    at = lambda p: int(datetime.fromisoformat(p["target_at"]).timestamp())  # noqa: E731
    until = lambda p: int(datetime.fromisoformat(p["until"]).timestamp())  # noqa: E731
    ours = lambda p, uuid, start=None: {  # noqa: E731
        "start": start or at(p),
        "stop": until(p),
        "uuid": uuid,
        "data": {"source": "constellation", "pass_id": p["entity"]["id"]},
    }
    have = [
        ours(kept, "u-kept"),
        ours(moved, "u-moved", start=at(moved) - 600),
        {"start": at(kept), "stop": until(kept), "uuid": "u-theirs", "data": {"note": "ops"}},
        {**ours(item(4, 10), "u-gone"), "data": {"source": "constellation", "pass_id": "p_gone"}},
    ]
    calls = calendar(monkeypatch, have)
    added, removed = module.Timeline().sync([kept, moved, new, item(60, 10)], NOW)
    assert ("timeline", "CONSTELLATION") in calls
    assert sorted(c[1] for c in calls if c[0] == "delete") == ["u-gone", "u-moved"]
    assert sorted(c[2] for c in calls if c[0] == "create") == ["p5", "p9"]
    assert all(c[1] == "reserve" for c in calls if c[0] == "create")
    assert (added, removed) == (2, 2)


def test_on_cosmos_core_the_timeline_is_left_alone(bridge, monkeypatch):
    """Core makes a timeline through its API but runs no timeline worker: COSMOS would start the
    Enterprise worker every few seconds. The bridge asks first and makes nothing."""
    module, _ = bridge
    calls = calendar(monkeypatch, [], enterprise=False)
    with pytest.raises(module.CoreHasNoTimelines):
        module.Timeline().sync([item(2, 10)], NOW)
    assert calls == [("info",)]
