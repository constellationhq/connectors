"""The COSMOS plugin's pieces beside the bridge: the CONSTELLATION_INT interface that carries out
APPROVE_SWAP and DECLINE_SWAP, the script helper, and the plugin files that add them only with
the approvals variable on. COSMOS's own modules are stood in for, as in test_cosmos_bridge.py."""

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bridges/cosmos"
LIB = PLUGIN / "targets/CONSTELLATION/lib"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Interface:
    """openc3.interfaces.interface.Interface, as much of it as the subclass uses."""

    def __init__(self) -> None:
        self.name = "CONSTELLATION_INT"
        self.write_count = 0

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


@pytest.fixture
def interface(monkeypatch):
    module = types.ModuleType("openc3.interfaces.interface")
    module.Interface = Interface  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openc3.interfaces.interface", module)
    monkeypatch.setitem(sys.modules, "openc3.utilities.secrets", None)
    monkeypatch.setenv("CONSTELLATION_API_KEY", "cos_live_k")
    return load("constellation_interface", LIB / "constellation_interface.py")


class Command:
    def __init__(self, name: str, **params: str) -> None:
        self.packet_name = name
        self.params = params

    def read(self, item: str) -> str:
        return self.params.get(item, "")


def test_approve_and_decline_post_the_decision_with_the_key(interface, monkeypatch):
    sent = []

    def urlopen(req, timeout=None):
        sent.append((req.get_method(), req.full_url, req.get_header("X-api-key")))
        return io.BytesIO(b'{"status": "approved"}')

    monkeypatch.setattr(interface.urllib.request, "urlopen", urlopen)
    it = interface.ConstellationInterface("https://api.test/")
    it.connect()
    it.write(Command("APPROVE_SWAP", RUN_ID="run_1", ITEM_ID="pass/1"))
    it.write(Command("DECLINE_SWAP", RUN_ID="run_1", ITEM_ID="pass_2"))
    assert sent == [
        ("POST", "https://api.test/predictions/actions/run_1/pass%2F1/approve", "cos_live_k"),
        ("POST", "https://api.test/predictions/actions/run_1/pass_2/reject", "cos_live_k"),
    ]
    assert it.write_count == 2 and it.read_allowed is False


def test_a_refused_decision_fails_the_command_with_what_to_do(interface, monkeypatch):
    answers = iter(
        [
            (403, {"error": "insufficient_scope", "required": "act:approve"}),
            (403, {"error": "plan_required", "plan": "pro", "feature": "act"}),
            (409, {"error": "action_decided"}),
            (404, {"error": "unknown_action"}),
        ]
    )

    def urlopen(req, timeout=None):
        status, body = next(answers)
        raise urllib.error.HTTPError(
            req.full_url, status, "", {}, io.BytesIO(json.dumps(body).encode())
        )  # type: ignore[arg-type]

    monkeypatch.setattr(interface.urllib.request, "urlopen", urlopen)
    it = interface.ConstellationInterface("https://api.test")
    it.connect()
    approve = Command("APPROVE_SWAP", RUN_ID="r", ITEM_ID="i")
    for words in ("lacks act:approve", "Operator + Act", "decided already", "read CONSTELLATION HELD"):
        with pytest.raises(RuntimeError, match=re.escape(words)):
            it.write(approve)
    with pytest.raises(RuntimeError, match="RUN_ID and ITEM_ID"):
        it.write(Command("APPROVE_SWAP", RUN_ID="r"))


def test_the_interface_sends_the_key_only_over_https_or_to_this_machine(interface):
    with pytest.raises(RuntimeError, match="not https"):
        interface.ConstellationInterface("http://api.example.com").connect()
    for url in ("http://localhost:4500", "http://127.0.0.1:4500", "https://api.constellation.space"):
        it = interface.ConstellationInterface(url)
        it.connect()
        assert it.connected()


@pytest.mark.skipif(shutil.which("ruby") is None, reason="needs ruby to render the ERB")
@pytest.mark.parametrize("approvals", ["off", "on"])
def test_the_commands_and_their_interface_come_only_with_approvals_on(approvals):
    def render(path: Path, **variables: str) -> str:
        script = (
            "b = binding; ARGV.each_slice(2) { |k, v| b.local_variable_set(k.to_sym, v) };"
            " print ERB.new(File.read(ENV['FILE']), trim_mode: '-').result(b)"
        )
        args = [x for kv in variables.items() for x in kv]
        return subprocess.run(
            ["ruby", "-rerb", "-e", script, *args],
            env={**os.environ, "FILE": str(path)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    on = approvals == "on"
    cmd = render(PLUGIN / "targets/CONSTELLATION/cmd_tlm/cmd.txt", approvals=approvals)
    assert ("COMMAND CONSTELLATION APPROVE_SWAP" in cmd) is on
    assert ("COMMAND CONSTELLATION DECLINE_SWAP" in cmd) is on
    assert cmd.count("HAZARDOUS") == (2 if on else 0)
    plugin = render(
        PLUGIN / "plugin.txt",
        constellation_api_url="https://api.test",
        consumer="cosmos-bridge",
        satellites="",
        outcomes="",
        timeline="off",
        approvals=approvals,
    )
    assert ("INTERFACE CONSTELLATION_INT constellation_interface.py https://api.test" in plugin) is on
    screen = render(
        PLUGIN / "targets/CONSTELLATION/screens/forecast.txt",
        satellites="sat-1=KESTREL1",
        approvals=approvals,
    )
    assert screen.count("BUTTON") == (2 if on else 0)
    assert "VALUE CONSTELLATION NEXT_PASS_KESTREL1 AOS" in screen and "<%" not in screen


# The script helper.


class Response:
    def __init__(self, status: int, doc: dict | None = None) -> None:
        self.status_code = status
        self.doc = doc

    def json(self) -> dict:
        return {"contents": base64.b64encode(json.dumps(self.doc).encode()).decode()}


def at(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def passed(pid: str, satellite: str, hours: float, p50: float, **extra) -> dict:
    gb = {"p10": p50 / 2, "p50": p50, "p90": p50 * 1.1}
    return {
        "entity": {
            "type": "pass",
            "id": pid,
            "satellite_id": satellite,
            "ground_station_id": "gs",
            "antenna_id": "ant-1",
        },
        "target_at": at(hours),
        "until": at(hours + 0.15),
        "value": gb,
        "explain": {
            "expected_gb": {"value": gb},
            "passes": {"value": 41.0, "unit": "deg", "source": "orbit"},
        },
        **extra,
    }


@pytest.fixture
def helper(monkeypatch):
    docs = {
        "index.json": {
            "workflows": [
                {"workflow": "modcod", "kind": "modcod-plan"},
                {"workflow": "pass-yield", "kind": "pass-yield"},
            ]
        },
        "workflow-pass-yield.json": {
            "items": [
                passed("p3", "sat-1", 3, 6.0),
                passed("p1", "sat-1", 1, 4.0),
                passed("p2", "sat-2", 2, 8.0),
                passed("gone", "sat-1", -2, 4.0),
                passed("far", "sat-1", 30, 4.0),
                passed("swap", "sat-1", 1.5, 4.0, action={"verb": "swap"}),
            ]
        },
        "workflow-modcod.json": {
            "items": [
                {
                    **passed("m1", "sat-1", 1, 4.0),
                    "series": [
                        {"t": at(1), "value": {"modcod": "QPSK 1/2", "sinr_db": 3.1}},
                        {"t": at(1.05), "value": {"modcod": "8PSK 3/4", "sinr_db": 8.2}},
                    ],
                }
            ]
        },
    }
    asked = []

    def request(method, endpoint, query=None, scope=None):
        asked.append((method, endpoint, query))
        name = endpoint.rsplit("/", 1)[1]
        return Response(200, docs[name]) if name in docs else Response(404)

    script = types.ModuleType("openc3.script")
    script.API_SERVER = types.SimpleNamespace(request=request)  # type: ignore[attr-defined]
    script.OPENC3_SCOPE = "DEFAULT"  # type: ignore[attr-defined]
    package = types.ModuleType("openc3")
    package.script = script  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openc3", package)
    monkeypatch.setitem(sys.modules, "openc3.script", script)
    return load("constellation_forecast", LIB / "constellation_forecast.py"), asked


def test_scripts_read_the_forecast_passes_soonest_first(helper):
    module, asked = helper
    passes = module.forecast_passes()
    assert [p["pass_id"] for p in passes] == ["p1", "p2", "p3"]
    assert [p["pass_id"] for p in module.forecast_passes("sat-1")] == ["p1", "p3"]
    first = passes[0]
    assert (first["expected_gb"], first["max_elevation"]) == (4.0, 41.0)
    # One expected GB: forecasts have no spread yet, so no p10 or p90 is handed on.
    assert not {"low_gb", "high_gb"} & set(first)
    assert isinstance(first["aos"], datetime) and first["aos"].tzinfo is not None
    assert module.next_forecast_pass("sat-2")["pass_id"] == "p2"
    assert module.next_forecast_pass("sat-9") is None
    assert asked[0] == (
        "get",
        "/openc3-api/storage/download_file/DEFAULT/constellation/index.json",
        {"bucket": "OPENC3_CONFIG_BUCKET"},
    )


def test_scripts_read_the_modcod_plan_step_by_step(helper):
    module, _ = helper
    (plan,) = module.modcod_plan("sat-1")
    assert [s["modcod"] for s in plan["steps"]] == ["QPSK 1/2", "8PSK 3/4"]
    assert plan["steps"][1]["sinr_db"] == 8.2 and isinstance(plan["steps"][0]["at"], datetime)
    assert module.forecast_document("missing") is None


PUBLIC = {
    "forecast_workflows",
    "forecast_document",
    "forecast_passes",
    "next_forecast_pass",
    "modcod_plan",
    "held_swap",
    "approve_held_swap",
    "decline_held_swap",
}


def test_the_helper_adds_no_name_that_could_replace_one_of_script_runners(helper):
    """load_utility runs the helper in Script Runner's own globals (running_script.py's), where a
    common name such as _time or datetime is COSMOS's own: a helper that defined _time broke every
    script that loaded it. Every name is public or starts with _constellation_."""
    module, _ = helper
    names = {n for n in vars(module) if not (n.startswith("__") and n.endswith("__"))}
    assert names - PUBLIC == {n for n in names if n.startswith("_constellation_")}
    assert PUBLIC <= names


def test_held_swap_reads_only_items_the_held_packet_has(helper):
    module, _ = helper
    tlm = (PLUGIN / "targets/CONSTELLATION/cmd_tlm/tlm.txt").read_text()
    block = tlm.split("TELEMETRY CONSTELLATION HELD ", 1)[1].split("\nTELEMETRY ", 1)[0]
    items = {x.split()[1] for x in block.splitlines() if x.strip().startswith("APPEND_ITEM")}
    assert set(module._constellation_held_items) <= items
