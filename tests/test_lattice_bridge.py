"""The Lattice bridge: antennas become taskable Lattice assets carrying Constellation's forecast,
and a ReservePass task is allowed or refused by that forecast, with its provenance uploaded.
Feed entries are built with the API's OTLP encoding (api.py), so this tests the published contract."""

import importlib.util
import io
import json
import os
import queue
import sys
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import api
import pytest
from api import item as spec_item
from api import output, to_otlp

SOURCE = Path(__file__).resolve().parents[1] / "clients/python/src/constellation_space/bridges/lattice/bridge.py"
NOW = datetime.now(UTC)


class Drained(queue.Queue):
    """Ends the bridge's worker loop once every queued task is taken."""

    def get(self, *args, **kwargs):
        if self.empty():
            raise SystemExit
        return super().get(*args, **kwargs)


class FakeLattice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.fail: dict[str, str] = {}
        self.replies: dict[str, Any] = {}

    def __call__(self, method, url, body: Any = None, headers=None, raw=None):
        path = url.split("/api/v1", 1)[-1]
        self.calls.append((method, path, json.loads(raw) if raw else body, headers))
        if path in self.fail:
            raise RuntimeError(f"{method} {path}: HTTP 400 {self.fail[path]}")
        return self.replies.get(path)

    def statuses(self) -> list[dict]:
        return [b["newStatus"] for _, p, b, _ in self.calls if p.endswith("/status")]

    def objects(self) -> list[tuple[str, dict]]:
        return [(p, b) for m, p, b, _ in self.calls if p.startswith("/objects/")]

    def entities(self) -> list[dict]:
        return [b for m, p, b, _ in self.calls if p == "/entities"]


ENV = {
    "CONSTELLATION_API_URL": "https://api.test",
    "CONSTELLATION_API_KEY": "k",
    "LATTICE_URL": "https://env.sandboxes.test",
    "LATTICE_TOKEN": "env-token",
    "LATTICE_SANDBOX_TOKEN": "account-token",
    "MIN_P10_GB": "5",
    "MAX_FORECAST_AGE_S": "21600",
}


@pytest.fixture(autouse=True)
def cursor_file(monkeypatch, tmp_path) -> Path:
    """Each test's bridge keeps its cursor, forecast and heartbeat in its own folder."""
    path = tmp_path / "cursor.json"
    monkeypatch.setenv("CURSOR_FILE", str(path))
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    return path


def load(monkeypatch, without: tuple[str, ...] = ()) -> Any:
    for key, value in ENV.items():
        monkeypatch.setitem(os.environ, key, value)
    for key in without:
        monkeypatch.delenv(key)
    spec = importlib.util.spec_from_file_location("lattice_bridge", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge(monkeypatch):
    module = load(monkeypatch)
    fake = FakeLattice()
    monkeypatch.setattr(module, "call", fake)
    return module, fake


def item(
    p10: float,
    sat: str = "sat-1",
    ant: str = "ant-1",
    start: timedelta = timedelta(hours=1),
    **facets,
) -> dict:
    at = NOW + start
    gb = {"p10": p10, "p50": p10 + 4, "p90": p10 + 5}
    return spec_item(
        {
            "type": "pass",
            "id": f"pass_{sat}_{ant}_{start}",
            "link_id": f"{sat}|{ant}",
            "satellite_id": sat,
            "ground_station_id": "gs-1",
            "antenna_id": ant,
            "source": "computed",
            "track": [
                {"t": at.isoformat(), "latitude_deg": 45.0, "longitude_deg": -2.0},
                {
                    "t": (at + timedelta(minutes=4)).isoformat(),
                    "latitude_deg": 52.5,
                    "longitude_deg": 4.0,
                },
                {
                    "t": (at + timedelta(minutes=8)).isoformat(),
                    "latitude_deg": 60.0,
                    "longitude_deg": 11.0,
                },
            ],
        },
        at.isoformat(),
        gb,
        until=(at + timedelta(minutes=8)).isoformat(),
        explain={
            # The ledger keys each entry by the node that wrote it; the orbit's is the input node.
            "passes": {"value": 40.0, "unit": "deg", "source": "orbit"},
            "snr": {"value": {"p50": 11.0}, "unit": "dB", "source": "snr"},
            "expected_gb": {"value": gb, "unit": "GB", "source": "expected_gb"},
        },
        workflow_version=4,
        **facets,
    )


QUANTILES = ("p10", "p50", "p90")


def entry(
    items, connection="lattice-bridge", workflow="w", cursor="c#run_1", made=NOW, **mapping
) -> dict:
    """A run's feed entry as the API publishes it, the run made at `made`: one resource per feed
    output. The output to `connection` is a Lattice one carrying the mapping fields given. Beside
    it rides another bridge's output with every pass priced at zero, which this bridge must never
    read."""
    lattice = output(connection, {"system": "lattice", **mapping})
    decoys = [
        {
            **i,
            "explain": {
                **i.get("explain", {}),
                "expected_gb": {"value": dict.fromkeys(QUANTILES, 0.0)},
            },
        }
        for i in items
    ]
    run = {"workflow": workflow, "run_id": cursor.split("#", 1)[1]}
    resources = [
        *to_otlp("t", items, made, run, lattice)["resourceLogs"],
        *to_otlp("t", decoys, made, run, output("yamcs-bridge"))["resourceLogs"],
    ]
    return {"cursor": cursor, "otlp": {"resourceLogs": resources}}


STATION = {
    "ground_station_id": "gs-1",
    "latitude_deg": 52.0,
    "longitude_deg": 4.4,
    "altitude_m": 10.0,
    "antenna_ids": ["ant-1", "ant-2"],
}
TLE = {
    "line1": "1 44714U 19074B   26016.00002315  .00082898  00000+0  44081-2 0  9990",
    "line2": "2 44714  53.0838  63.0449 0001350 116.5282 165.6258 15.14557594  5744",
}
DOCUMENT = {
    "ground_stations": {
        "gs-1": {
            "latitude_deg": 52.0,
            "longitude_deg": 4.4,
            "altitude_m": 10.0,
            "antennas": {"ant-1": {"min_elevation_deg": 10}, "ant-2": {"min_elevation_deg": 25}},
        }
    },
    "satellites": {"sat-1": {"tle": TLE}, "sat-2": {"norad_id": 1}},
}


def served(module, lattice, antenna="ant-1") -> dict:
    """The last-published forecast service entity for `antenna`."""
    return [e for e in lattice.entities() if e["entityId"] == module.entity_id(antenna)][-1]


def swap_of(healthy: dict, deadline: timedelta = timedelta(minutes=30)) -> dict:
    """The rain-fade advice for a pass: the same pass, a warning, with a swap to ant-2."""
    return {
        **healthy,
        "explain": {**healthy["explain"], "swap": {"value": 2.0, "unit": "GB"}},
        "severity": "warning",
        "action": {
            "verb": "swap",
            "from": healthy["entity"]["id"],
            "to": "pass_2",
            "to_antenna_id": "ant-2",
            "to_at": "2026-11-06T01:00:00Z",
            "deadline": (NOW + deadline).isoformat(),
            "mode": "suggest",
            "gain_gb_p10": 2.0,
        },
    }


def task(spec: dict, agent: str) -> dict:
    return {
        "version": {"taskId": "task-1", "statusVersion": 1},
        "specification": {
            "@type": "type.googleapis.com/constellation.tasks.v1.ReservePass",
            **spec,
        },
        "relations": {"assignee": {"system": {"entityId": agent}}},
    }


def test_a_pass_with_a_healthy_tail_is_allowed_and_leaves_provenance(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    assert module.deliver(entry([item(12.4)]), forecast) == (1, 0, 0, 0)
    agent = module.entity_id("ant-1")
    decision = module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    assert decision["decision"] == "ALLOW"
    assert [s["status"] for s in lattice.statuses()] == ["STATUS_EXECUTING", "STATUS_DONE_OK"]
    ((path, provenance),) = lattice.objects()
    assert path == "/objects/constellation-decisions-allow-task-1.json"
    assert (
        provenance["expected_gb"]["p10"] == 12.4
        and provenance["provenance"]["workflow_version"] == 4
    )
    versions = [b["statusVersion"] for _, p, b, _ in lattice.calls if p.endswith("/status")]
    assert versions == [2, 3]


def test_an_allowed_task_says_nothing_was_reserved(bridge):
    """The bridge decides; it books nothing. DONE_OK must not read as a reservation made."""
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4)]), forecast)
    agent = module.entity_id("ant-1")
    module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    done = lattice.statuses()[-1]
    assert done["status"] == "STATUS_DONE_OK"
    outcome = done["result"]["value"]
    assert outcome["outcome"] == "ALLOWED_NOT_RESERVED" and outcome["reserved"] is False
    assert "Nothing was reserved" in outcome["summary"]


def test_a_denied_task_names_its_outcome_and_reserves_nothing(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(3.1)]), forecast)
    agent = module.entity_id("ant-1")
    module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    outcome = lattice.statuses()[-1]["result"]["value"]
    assert outcome["outcome"] == "DENIED" and outcome["reserved"] is False


def test_a_forecast_older_than_the_max_age_is_refused_as_stale(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    made = NOW - timedelta(seconds=21600 + 60)
    module.deliver(entry([item(12.4, start=timedelta(hours=8))], made=made), forecast)
    agent = module.entity_id("ant-1")
    decision = module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    assert decision["decision"] == "DENY" and decision["expected_gb"]["p10"] == 12.4
    assert decision["diagnostic"].startswith("STALE_FORECAST: ")
    assert "MAX_FORECAST_AGE_S=21600" in decision["diagnostic"]
    assert decision["max_forecast_age_s"] == 21600
    done = lattice.statuses()[-1]
    assert done["status"] == "STATUS_DONE_NOT_OK"
    assert done["taskError"]["message"].startswith("STALE_FORECAST")


def test_a_forecast_just_inside_the_max_age_still_decides(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    made = NOW - timedelta(seconds=21600 - 60)
    module.deliver(entry([item(12.4, start=timedelta(hours=8))], made=made), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW"
    made_at = module.when(decision["provenance"]["made_at"])
    assert abs(made_at - made) < timedelta(milliseconds=1)


def test_a_kept_forecast_of_unknown_age_is_refused_as_stale(bridge):
    """A forecast kept by a bridge that did not record when it was made never decides."""
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4)]), forecast)
    saved = forecast.saved(NOW)
    for kept in saved["passes"]["ant-1"]:
        del kept["provenance"]["made_at"]
    restored = module.Forecast()
    restored.restore(saved)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", restored, NOW)
    assert decision["decision"] == "DENY"
    assert decision["diagnostic"].startswith("STALE_FORECAST: ")


def test_a_thin_tail_is_refused_with_the_diagnostic_and_no_retask(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(3.1)]), forecast)
    agent = module.entity_id("ant-1")
    module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    executing, done = lattice.statuses()
    assert done["status"] == "STATUS_DONE_NOT_OK"
    assert done["taskError"]["code"] == "ERROR_CODE_REJECTED"
    assert done["taskError"]["message"].startswith(
        "EXPECTED_GB_BELOW_FLOOR: p10=3.1 GB < min=5.0 GB"
    )
    assert not [p for m, p, _, _ in lattice.calls if m == "POST" and p == "/tasks"]


def test_no_forecast_for_the_pass_is_a_refusal_not_a_guess(bridge):
    module, lattice = bridge
    agent = module.entity_id("ant-1")
    decision = module.handle(
        task({"satelliteId": "sat-9"}, agent), {agent: "ant-1"}, module.Forecast()
    )
    assert decision["decision"] == "DENY" and decision["diagnostic"].startswith("NO_FORECAST")


def test_a_task_naming_an_aos_picks_that_pass(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.0), item(2.0, start=timedelta(hours=3))]), forecast)
    later = (NOW + timedelta(hours=3, minutes=2)).isoformat()
    decision = module.decide(
        task({"satelliteId": "sat-1", "aos": later}, "x"), "ant-1", forecast, NOW
    )
    assert decision["decision"] == "DENY" and decision["expected_gb"]["p10"] == 2.0


def test_a_rain_fade_swap_rides_along_as_an_advisory(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW" and decision["advisory"].startswith(
        "rain-fade risk: book sat-1 on ant-2"
    )


def test_runs_for_other_connections_are_ignored(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    assert module.deliver(entry([item(12.0)], connection="yamcs-bridge"), forecast) == (0, 0, 0, 0)
    assert forecast.passes == {}


def test_only_this_connections_output_of_a_run_is_read(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    # The entry also carries the yamcs-bridge output, every pass priced at zero.
    assert module.deliver(entry([item(12.0)], min_p10_gb=6), forecast) == (1, 0, 0, 0)
    found = forecast.find("ant-1", "sat-1", None, NOW)
    assert module.gb_of(found)["p10"] == 12.0
    made_at = found["provenance"].pop("made_at")
    assert abs(module.when(made_at) - NOW) < timedelta(milliseconds=1)
    assert found["provenance"] == {"workflow": "w", "run_id": "run_1", "workflow_version": 4}
    assert forecast.mapping("ant-1")[0]["min_p10_gb"] == 6


def test_check_in_names_the_lattice_system_and_the_mapping_version(bridge):
    module, lattice = bridge
    module.check_in()
    ((method, path, body, _),) = lattice.calls
    assert (method, path) == ("POST", "https://api.test/predictions/feed/checkin")
    assert body["system"] == "lattice" and body["mapping_versions"] == [2]
    assert body["consumer"] == "lattice-bridge"


def test_antennas_are_taskable_ground_stations_grouped_by_site_with_both_tokens(
    bridge, monkeypatch
):
    module, lattice = bridge
    monkeypatch.setattr(module, "PUBLISH_STATIONS", True)
    forecast = module.Forecast()
    module.deliver(entry([item(12.4), item(8.0, ant="ant-2", start=timedelta(hours=3))]), forecast)
    module.publish([STATION], forecast, {})
    site, ant1, pass1, ant2, _ = lattice.entities()
    assert site["entityId"] == module.site_id("gs-1")
    # RADAR is the Lattice UI icon for a ground dish; an unknown platform type draws a dot.
    assert site["ontology"]["platformType"] == "RADAR"
    assert site["ontology"]["template"] == "TEMPLATE_ASSET"
    assert [r["relatedEntityId"] for r in site["relationships"]["relationships"]] == [
        module.entity_id("ant-1"),
        module.entity_id("ant-2"),
    ]
    assert ant1["entityId"] == module.entity_id("ant-1")
    assert ant1["ontology"]["template"] == "TEMPLATE_ASSET"
    assert site["symbology"]["milStd2525C"]["sidc"] == "SFGPUUSRS------"
    assert site["location"]["position"]["latitudeDegrees"] == 52.0
    # Antennas carry no location to pile on the station; a pass is drawn along its ground track.
    assert not any("location" in e for e in (ant1, ant2))
    assert pass1["ontology"]["template"] == "TEMPLATE_GEO"
    (parent,) = ant1["relationships"]["relationships"]
    assert (
        parent["relatedEntityId"] == site["entityId"]
        and "groupParent" in parent["relationshipType"]
    )
    # The catalog advertises what Lattice can resolve: a Struct, its fields in the description.
    (offered,) = ant1["taskCatalog"]["taskDefinitions"]
    assert offered["taskSpecificationUrl"] == "type.googleapis.com/google.protobuf.Struct"
    assert "satelliteId" in ant1["description"]
    (row,) = ant1["health"]["components"]
    assert row["name"].startswith("Pass sat-1 · ") and "16.4 GB (p10 12.4)" in row["name"]
    assert ant1["health"]["healthStatus"] == "HEALTH_STATUS_HEALTHY"
    assert ant1["health"]["activeAlerts"] == []
    headers = lattice.calls[0][3]
    assert headers["Authorization"] == "Bearer env-token"
    assert headers["Anduril-Sandbox-Authorization"] == "Bearer account-token"


def test_a_decided_task_carries_the_decision_as_its_result(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy, thin = item(9.0), item(3.1, sat="sat-2")
    module.deliver(entry([healthy, thin]), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    agent = module.entity_id("ant-1")
    module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    module.handle(task({"satelliteId": "sat-2"}, agent), {agent: "ant-1"}, forecast)
    allowed, denied = (s["result"] for s in lattice.statuses() if "result" in s)
    assert allowed["@type"] == "type.googleapis.com/google.protobuf.Struct"
    ok = allowed["value"]
    assert ok["decision"] == "ALLOW" and ok["expectedGb"]["p10"] == 9.0 and ok["minP10Gb"] == 5.0
    assert ok["summary"].startswith("ALLOW sat-1 on ant-1 AOS ")
    assert "p10 9.0 / p50 13.0 / p90 14.0 GB" in ok["summary"]
    assert "book sat-1 on ant-2" in ok["summary"] and "cancel by" in ok["advisory"]
    assert denied["value"]["decision"] == "DENY"
    assert "EXPECTED_GB_BELOW_FLOOR" in denied["value"]["summary"]


def test_no_forecast_still_leaves_a_readable_result(bridge):
    module, lattice = bridge
    agent = module.entity_id("ant-1")
    module.handle(task({"satelliteId": "sat-9"}, agent), {agent: "ant-1"}, module.Forecast())
    (outcome,) = (s["result"]["value"] for s in lattice.statuses() if "result" in s)
    assert outcome["summary"].startswith("DENY sat-9 on ant-1; floor p10 5.0 GB. NO_FORECAST")


def test_an_antenna_shows_its_two_soonest_swaps_and_counts_the_rest(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    passes = [item(9.0, start=timedelta(hours=h)) for h in (3, 1, 2, 4)]
    module.deliver(entry(passes), forecast)
    module.deliver(entry([swap_of(p) for p in passes]), forecast)
    module.publish([STATION], forecast, {})
    antenna = served(module, lattice)
    alerts = antenna["health"]["activeAlerts"]
    assert [a["description"].split(" at ")[1][:16] for a in alerts] == [
        passes[1]["target_at"][:16],
        passes[2]["target_at"][:16],
    ]
    more = antenna["health"]["components"][-1]
    assert more["name"] == "2 more rain-fade swaps open"


def test_a_task_made_over_rest_carries_its_fields_in_a_struct(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4)]), forecast)
    agent = module.entity_id("ant-1")
    rest = task({}, agent)
    rest["specification"] = {
        "@type": "type.googleapis.com/google.protobuf.Struct",
        "value": {"satelliteId": "sat-1"},
    }
    assert module.handle(rest, {agent: "ant-1"}, forecast)["decision"] == "ALLOW"


def test_the_floor_is_the_workflows_whose_forecast_decided(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(8.0, ant="ant-1")], min_p10_gb=10), forecast)
    module.deliver(entry([item(3.0, ant="ant-2")], workflow="v", min_p10_gb=2), forecast)
    strict = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    loose = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-2", forecast, NOW)
    assert (strict["decision"], strict["min_p10_gb"], strict["floor_from"]) == (
        "DENY",
        10,
        "workflow",
    )
    assert strict["diagnostic"].startswith("EXPECTED_GB_BELOW_FLOOR: p10=8.0 GB < min=10.0 GB")
    assert (loose["decision"], loose["min_p10_gb"]) == ("ALLOW", 2)


def test_an_output_for_another_system_is_refused_and_counted_never_applied(bridge, capsys):
    """An output to this connection that is not a Lattice one (a Yamcs output, as one saved
    without a mapping is) never reaches Lattice: the plan that allows it is not Lattice's."""
    module, _ = bridge
    forecast = module.Forecast()
    run = {"workflow": "w", "run_id": "run_1"}
    yamcs = to_otlp("t", [item(12.0)], NOW, run, output("lattice-bridge"))
    assert module.deliver({"cursor": "c#run_1", "otlp": yamcs}, forecast) == (0, 1, 0, 0)
    assert forecast.antennas() == set()
    assert "refused an output of run run_1: it is a yamcs output" in capsys.readouterr().err
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert (decision["decision"], decision["min_p10_gb"], decision["floor_from"]) == (
        "DENY",
        5.0,
        "env",
    )


def test_a_mapping_naming_antennas_keeps_only_their_passes(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    used, *_ = module.deliver(
        entry([item(9.0), item(9.0, ant="ant-2")], antennas=["ant-2"]), forecast
    )
    assert used == 1 and set(forecast.passes) == {"ant-2"}


def test_advisories_off_leaves_the_swap_off_the_decision_and_the_antenna(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy], advisories=False), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert "advisory" not in decision
    module.publish([STATION], forecast, {})
    assert served(module, lattice)["health"]["activeAlerts"] == []


def test_an_open_swap_puts_a_rain_fade_alert_on_the_antenna_until_it_closes(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    module.publish([STATION], forecast, {})
    health = served(module, lattice)["health"]
    assert health["healthStatus"] == "HEALTH_STATUS_HEALTHY"
    (alert,) = health["activeAlerts"]
    assert alert["level"] == "ALERT_LEVEL_CAUTION"
    assert "book sat-1 on ant-2" in alert["description"] and "cancel by" in alert["description"]
    # The next swap run advises nothing for it, and a passed deadline closes a swap too.
    module.deliver(entry([swap_of(item(9.0, ant="ant-2"))]), forecast)
    assert forecast.open_swaps("ant-1", NOW) == []
    module.deliver(entry([swap_of(healthy, timedelta(minutes=-1))]), forecast)
    assert forecast.open_swaps("ant-1", NOW) == []


def test_listed_passes_are_their_own_entities_until_los_and_withdrawn_when_dropped(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    passes = [item(9.0, start=timedelta(hours=h)) for h in (1, 2, 3)]
    module.deliver(entry(passes, passes_listed=2), forecast)
    shown: dict = {}
    module.publish([STATION], forecast, shown)
    listed = [e for e in lattice.entities() if e["ontology"]["platformType"] == "Forecast Pass"]
    assert [e["entityId"] for e in listed] == [
        module.pass_entity_id(p["entity"]["id"]) for p in passes[:2]
    ]
    first = listed[0]
    assert first["provenance"]["dataType"] == "constellation-forecast"
    assert "not a scheduled contact" in first["description"]
    assert first["aliases"]["name"].endswith("13.0 GB (p10 9.0)")
    assert module.when(first["expiryTime"]) > module.when(passes[0]["until"])
    (window,) = first["schedules"]["schedules"][0]["windows"]
    assert window["durationMillis"] == str(8 * 60 * 1000)
    (antenna,) = first["relationships"]["relationships"]
    assert antenna["relatedEntityId"] == module.entity_id("ant-1")
    # Republishing updates the same ids; a pass the next run drops is withdrawn.
    module.deliver(entry(passes[1:], passes_listed=2), forecast)
    lattice.calls.clear()
    module.publish([STATION], forecast, shown)
    withdrawn = [e for e in lattice.entities() if e["isLive"] is False]
    assert [e["entityId"] for e in withdrawn] == [module.pass_entity_id(passes[0]["entity"]["id"])]


def test_pass_entities_off_publishes_only_the_forecast_services(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(9.0)], pass_entities=False), forecast)
    module.publish([STATION], forecast, {})
    assert {e["ontology"]["platformType"] for e in lattice.entities()} == {"Constellation Forecast"}


def test_an_audit_ledger_run_does_not_replace_the_forecast(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(9.0)]), forecast)
    past = item(1.0, start=timedelta(hours=-5))
    ledger = {"possible_gb": 5.0, "delivered_gb": 1.0, "status": "short"}
    past["explain"] = {**past["explain"], "ledger": {"value": ledger}}
    module.deliver(entry([past]), forecast)
    found = forecast.find("ant-1", "sat-1", None, NOW)
    assert module.gb_of(found)["p10"] == 9.0


def test_a_task_that_fails_midway_is_closed_with_the_lattice_error_not_lost(bridge, capsys):
    module, lattice = bridge
    forecast = module.Forecast()
    agent = module.entity_id("ant-1")
    lattice.fail["/objects/constellation-decisions-deny-task-1.json"] = '{"code":"UNAVAILABLE"}'
    lattice.replies["/tasks/task-1"] = {
        "status": {"status": "STATUS_EXECUTING"},
        "version": {"taskId": "task-1", "statusVersion": 2},
    }
    tasks = Drained()
    one = task({"satelliteId": "sat-1"}, agent)
    tasks.put(one)
    tasks.put(one)  # a reconnected stream delivering it again
    with pytest.raises(SystemExit):
        module.work(tasks, {agent: "ant-1"}, forecast)
    executing, failed = lattice.statuses()
    assert executing["status"] == "STATUS_EXECUTING"
    assert failed["status"] == "STATUS_DONE_NOT_OK"
    assert failed["taskError"]["code"] == "ERROR_CODE_FAILED"
    assert failed["taskError"]["message"].startswith("BRIDGE_ERROR: POST /objects/")
    err = capsys.readouterr().err
    assert "task task-1" in err and '{"code":"UNAVAILABLE"}' in err


def test_items_in_a_shape_newer_than_the_bridge_reads_are_skipped_not_misread(
    bridge, monkeypatch, capsys
):
    module, _ = bridge
    monkeypatch.setattr(api, "SCHEMA_VERSION", "constellation.item/v3")
    forecast = module.Forecast()
    assert module.deliver(entry([item(12.0)], min_p10_gb=6), forecast) == (0, 0, 0, 0)
    assert forecast.passes == {}
    assert "constellation.item/v3" in capsys.readouterr().out


def test_a_pass_is_a_line_along_its_ground_track_in_time_order(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4)]), forecast)
    module.publish([STATION], forecast, {})
    (line,) = [
        e for e in lattice.entities() if e["ontology"].get("platformType") == "Forecast Pass"
    ]
    positions = line["geoShape"]["line"]["positions"]
    assert [(p["latitudeDegrees"], p["longitudeDegrees"]) for p in positions] == [
        (45.0, -2.0),
        (52.5, 4.0),
        (60.0, 11.0),
    ]
    assert "location" not in line and line["aliases"]["name"].endswith("16.4 GB (p10 12.4)")
    # A pass with no track (the schema makes it optional) is still a point at its station.
    bare = item(12.4)
    del bare["entity"]["track"]
    point = module.pass_entity(bare, module.position(STATION), NOW)
    assert point["geoShape"]["point"]["position"]["latitudeDegrees"] == 52.0


def test_max_elevation_is_found_by_source_whatever_node_wrote_it(bridge):
    module, lattice = bridge
    assert module.results.max_elevation(item(12.4)) == 40.0
    forecast = module.Forecast()
    module.deliver(entry([item(12.4)]), forecast)
    module.publish([STATION], forecast, {})
    (row,) = served(module, lattice)["health"]["components"]
    assert "max elevation 40" in row["messages"][0]["message"]


def test_a_pass_below_the_floor_is_an_advisory_never_the_services_health(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4), item(2.0, sat="sat-2", start=timedelta(hours=2))]), forecast)
    module.publish([STATION], forecast, {})
    health = served(module, lattice)["health"]
    rows = health["components"]
    assert {r["health"] for r in rows} == {"HEALTH_STATUS_HEALTHY"}
    assert rows[1]["name"].endswith("· below floor")
    assert "below the 5.0 GB floor" in rows[1]["messages"][0]["message"]
    (advisory,) = health["activeAlerts"]
    assert advisory["level"] == "ALERT_LEVEL_ADVISORY"
    assert advisory["alertCode"] == "CONSTELLATION_LOW_YIELD_FORECAST"
    assert advisory["description"].startswith("Forecast: sat-2 at ")


def test_daily_capacity_stays_with_its_antenna_and_each_modcod_plan_with_its_pass(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    capacity = spec_item(
        {"type": "antenna", "id": "ant-2", "ground_station_id": "gs-1"},
        day.isoformat(),
        {"passes": 3, "sellable_min": 24.0, "p10": 20.0, "p50": 31.5, "p90": 40.0},
        until=(day + timedelta(days=1)).isoformat(),
        workflow_version=1,
    )
    planned = item(12.4, ant="ant-1", start=timedelta(hours=3))
    aos = planned["target_at"]
    later = (when_of(aos) + timedelta(minutes=3)).isoformat()
    plan = {
        **planned,
        "series": [
            {"t": aos, "value": {"modcod": "QPSK 1/2", "sinr_db": 2.1}},
            {"t": later, "value": {"modcod": "8PSK 2/3", "sinr_db": 7.9}},
        ],
    }
    module.deliver(entry([capacity, plan]), forecast)
    module.publish([STATION], forecast, {})
    by_id = {e["entityId"]: e for e in lattice.entities()}
    ant1, ant2 = by_id[module.entity_id("ant-1")], by_id[module.entity_id("ant-2")]
    (modcod,) = ant1["health"]["components"]
    assert modcod["id"] == f"modcod|{plan['entity']['id']}"
    assert modcod["name"] == f"MODCOD sat-1 · {aos[11:16]}Z · 1 change"
    message = modcod["messages"][0]["message"]
    assert message == f"QPSK 1/2 at {aos[11:16]}Z → 8PSK 2/3 at {later[11:16]}Z"
    (cap,) = ant2["health"]["components"]
    assert cap["id"] == f"capacity|{day.date()}"
    assert cap["name"] == f"Capacity {day.date()} · 31.5 GB · 3 passes"


def test_coverage_is_an_opt_in_forecast_circle_from_the_masks_and_the_fleets_altitude(
    bridge, monkeypatch
):
    module, lattice = bridge
    (station,) = module.stations_of(DOCUMENT)
    assert station["antenna_ids"] == ["ant-1", "ant-2"]
    low, high = station["coverage_m"]["ant-1"], station["coverage_m"]["ant-2"]
    # About 550 km up, a 10 degree mask sees ~1,700 km out and a 25 degree one about half that.
    assert 1_500_000 < low < 1_900_000 and high < low * 0.6
    module.deliver(entry([item(9.0)]), forecast := module.Forecast())
    module.publish([station], forecast, {})
    assert not [e for e in lattice.entities() if "coverage" in e["aliases"]["name"]]
    monkeypatch.setattr(module, "PUBLISH_COVERAGE", True)
    module.publish([station], forecast, {})
    (circle,) = [e for e in lattice.entities() if "coverage" in e["aliases"]["name"]]
    assert circle["aliases"]["name"] == "Forecast · coverage · gs-1"
    assert circle["geoShape"]["ellipse"]["semiMajorAxisM"] == low
    assert circle["location"]["position"]["latitudeDegrees"] == 52.0


def test_satellites_with_an_element_set_are_published_with_their_orbit(bridge):
    module, _ = bridge
    (sat,) = module.satellite_entities(DOCUMENT, NOW)
    elements = sat["orbit"]["orbitMeanElements"]
    kepler, tle = elements["meanKeplerianElements"], elements["tleParameters"]
    assert sat["aliases"]["name"] == "sat-1" and sat["ontology"]["platformType"] == "SATELLITE"
    assert kepler["meanMotion"] == 15.14557594 and kepler["inclinationDeg"] == 53.0838
    assert kepler["eccentricity"] == 0.000135 and kepler["epoch"].startswith("2026-01-16T00:00:02")
    assert tle["noradCatId"] == 44714 and tle["bstar"] == pytest.approx(0.0044081)
    assert elements["metadata"]["meanElementTheory"] == "MEAN_ELEMENT_THEORY_SGP4"


def test_an_open_swap_puts_a_rain_hazard_on_the_station_and_takes_it_down_when_closed(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    shown: dict = {}
    module.publish([STATION], forecast, shown)
    hazards = [
        e for e in lattice.entities() if e.get("geoDetails", {}).get("type") == "GEO_TYPE_HAZARD"
    ]
    (hazard,) = hazards
    assert hazard["location"]["position"]["latitudeDegrees"] == 52.0
    assert hazard["geoShape"]["ellipse"]["semiMajorAxisM"] > 0 and hazard["isLive"]
    assert hazard["aliases"]["name"].startswith("Forecast · rain fade · sat-1 @ ant-1")
    forecast.swaps = {}
    lattice.calls.clear()
    module.publish([STATION], forecast, shown)
    (gone,) = [e for e in lattice.entities() if e["entityId"] == hazard["entityId"]]
    assert gone["isLive"] is False


def when_of(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_the_forecast_is_a_named_service_never_the_antenna_itself(bridge):
    """C2's asset panel shows the name, description and alerts, not the status rows; none of
    them may pass for the customer's own antenna."""
    module, lattice = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.4), item(2.0, sat="sat-2", start=timedelta(hours=2))]), forecast)
    module.publish([STATION], forecast, {})
    service = served(module, lattice)
    assert service["aliases"]["name"] == "Constellation forecast · ant-1"
    assert service["ontology"]["platformType"] == "Constellation Forecast"
    assert "location" not in service and "relationships" not in service
    assert service["health"]["healthStatus"] == "HEALTH_STATUS_HEALTHY"
    text = service["description"]
    assert text.startswith("Constellation's forecast for antenna ant-1 at gs-1, not the antenna")
    assert "16.4 GB (p10 12.4) ok" in text and "6.0 GB (p10 2.0) below the 5.0 GB floor" in text
    assert "ReservePass" in text
    # Every forecast shape on the map reads as one and shares the forecast colour.
    (line,) = [
        e for e in lattice.entities() if e["ontology"].get("platformType") == "Forecast Pass"
    ][:1]
    assert line["aliases"]["name"].startswith("Forecast · sat-1 @ ant-1 ")
    assert line["geoDetails"]["visualDetails"]["lineColor"] == module.FORECAST_COLOR


def test_satellites_are_published_only_where_lattice_has_none_of_its_own(bridge, monkeypatch):
    module, lattice = bridge
    satellites = module.satellite_entities(DOCUMENT, NOW)
    module.publish([STATION], module.Forecast(), {}, satellites)
    assert not [e for e in lattice.entities() if "orbit" in e]
    monkeypatch.setattr(module, "PUBLISH_SATELLITES", True)
    module.publish([STATION], module.Forecast(), {}, satellites)
    assert [e["aliases"]["name"] for e in lattice.entities() if "orbit" in e] == ["sat-1"]


def test_a_satellite_is_propagated_from_its_elements_to_where_it_is_now(bridge):
    """Lattice draws an entity where its location says, never from its orbit: the bridge moves
    each satellite itself. Checked against what the elements fix: a ~550 km orbit at 53 degrees,
    moving at orbital speed, its ground track drifting west as the Earth turns under it."""
    module, _ = bridge
    epoch = datetime(2026, 1, 16, 0, 0, 2, tzinfo=UTC)
    seen = [module.propagate(TLE, epoch + timedelta(minutes=m)) for m in range(0, 24 * 60, 7)]
    assert all(520_000 < s["altitude_m"] < 580_000 for s in seen)
    assert max(abs(s["latitude_deg"]) for s in seen) == pytest.approx(53.08, abs=0.6)
    speeds = [sum(v * v for v in s["velocity_enu"].values()) ** 0.5 for s in seen]
    assert all(6_900 < v < 7_800 for v in speeds)  # m/s over the turning Earth
    # One orbit (~95 min) later it is back at the same latitude, about 24 degrees further west.
    period = timedelta(minutes=1440 / 15.14557594)
    a, b = module.propagate(TLE, epoch), module.propagate(TLE, epoch + period)
    assert b["latitude_deg"] == pytest.approx(a["latitude_deg"], abs=1.0)
    assert ((a["longitude_deg"] - b["longitude_deg"]) % 360) == pytest.approx(23.9, abs=1.5)


def test_a_satellite_sgp4_cannot_place_is_skipped_with_a_warning(bridge, monkeypatch, capsys):
    module, lattice = bridge
    monkeypatch.setattr(module, "PUBLISH_SATELLITES", True)
    document = {**DOCUMENT, "satellites": {"sat-1": {"tle": TLE}, "sat-2": {"tle": TLE}}}
    satellites = module.satellite_entities(document, NOW)
    # A year past its epoch, the fixture's drag term has decayed the orbit: SGP4 error 6.
    assert module.publish_satellites(satellites, NOW + timedelta(days=365)) == 0
    assert "sat-1 not published" in capsys.readouterr().err
    assert module.publish_satellites(satellites[1:], datetime(2026, 1, 17, tzinfo=UTC)) == 1


def test_publishing_satellites_without_sgp4_stops_at_start(monkeypatch, capsys):
    monkeypatch.setitem(ENV, "PUBLISH_SATELLITES", "true")
    monkeypatch.setitem(sys.modules, "sgp4", None)
    with pytest.raises(SystemExit) as stopped:
        started(monkeypatch, Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [LISTED]})).ready()
    assert stopped.value.code == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert "PUBLISH_SATELLITES needs the sgp4 package" in line


def test_published_satellites_carry_where_they_are_now(bridge, monkeypatch):
    module, lattice = bridge
    monkeypatch.setattr(module, "PUBLISH_SATELLITES", True)
    # publish places satellites at the wall clock, so the element set is dated now: the
    # fixture's drag term would decay its orbit within months of its own epoch.
    day = NOW.timetuple().tm_yday + (
        NOW - NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    ) / timedelta(days=1)
    fresh = {**TLE, "line1": f"{TLE['line1'][:18]}{NOW:%y}{day:012.8f}{TLE['line1'][32:]}"}
    document = {**DOCUMENT, "satellites": {"sat-1": {"tle": fresh}}}
    module.publish([STATION], module.Forecast(), {}, module.satellite_entities(document, NOW))
    (sat,) = [e for e in lattice.entities() if "orbit" in e]
    position = sat["location"]["position"]
    assert 500_000 < position["altitudeHaeMeters"] < 600_000
    assert set(sat["location"]["velocityEnu"]) == {"e", "n", "u"}


def test_only_what_a_workflow_forecasts_reaches_lattice(bridge, monkeypatch):
    """The network config lists everything the account has, canaries and test antennas too;
    Lattice shows only the antennas a workflow sends this bridge forecasts for, and their
    stations."""
    module, lattice = bridge
    monkeypatch.setattr(module, "PUBLISH_STATIONS", True)
    canary = {**STATION, "ground_station_id": "slo-canary", "antenna_ids": ["smoke-ant"]}
    forecast = module.Forecast()
    module.publish([STATION, canary], forecast, {})
    assert lattice.entities() == []
    module.deliver(entry([item(9.0)]), forecast)
    module.publish([STATION, canary], forecast, {})
    published = {e["entityId"] for e in lattice.entities()}
    assert module.site_id("gs-1") in published and module.entity_id("ant-1") in published
    assert module.site_id("slo-canary") not in published
    assert module.entity_id("smoke-ant") not in published
    # ant-2 shares the station but no workflow forecasts it.
    assert module.entity_id("ant-2") not in published


def test_a_swap_run_that_advises_nothing_takes_the_rain_fade_advice_down(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([swap_of(healthy)]), forecast)
    assert forecast.open_swaps("ant-1", NOW)
    run = {"workflow": "w", "run_id": "run_2", "template": "rain-fade-swap"}
    nothing = to_otlp(
        "t",
        [],
        NOW,
        run,
        output("lattice-bridge", {"system": "lattice"}),
    )
    module.deliver({"cursor": "c#run_2", "otlp": nothing}, forecast)
    assert forecast.open_swaps("ant-1", NOW) == []
    # The passes the last pass run forecast are untouched.
    assert forecast.find("ant-1", "sat-1", None, NOW) is not None


def test_a_pass_at_risk_with_no_backup_is_advice_lattice_neither_draws_nor_enforces(bridge):
    """A rain-fade swap with no backup arrives as an advisory (a warning and a note, no action):
    not a forecast pass of the swap workflow beside the pass run's, and nothing to deny."""
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy], workflow="yield"), forecast)
    advisory = {**healthy, "severity": "warning", "note": "No backup pass in the window"}
    module.deliver(entry([advisory], workflow="rain", cursor="c#run_2"), forecast)
    assert [p["provenance"]["workflow"] for p in forecast.passes["ant-1"]] == ["yield"]
    assert forecast.open_swaps("ant-1", NOW) == [] and forecast.held == {}


def empty_swap_run(workflow: str, **mapping) -> dict:
    run = {"workflow": workflow, "run_id": "run_9", "template": "rain-fade-swap"}
    lattice = output("lattice-bridge", {"system": "lattice", **mapping})
    return {"cursor": "c#run_9", "otlp": to_otlp("t", [], NOW, run, lattice)}


def test_a_swap_run_takes_down_only_its_own_workflows_advice(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    one, two = item(9.0), item(9.0, ant="ant-2")
    module.deliver(entry([swap_of(one)], workflow="rain-a"), forecast)
    module.deliver(entry([swap_of(two)], workflow="rain-b"), forecast)
    module.deliver(empty_swap_run("rain-b"), forecast)
    assert [s["entity"]["id"] for s in forecast.open_swaps("ant-1", NOW)] == [one["entity"]["id"]]
    assert forecast.open_swaps("ant-2", NOW) == []


def test_a_swap_run_on_some_antennas_leaves_its_advice_on_the_others(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    one, two = item(9.0), item(9.0, ant="ant-2")
    module.deliver(entry([swap_of(one), swap_of(two)], workflow="rain"), forecast)
    module.deliver(empty_swap_run("rain", antennas=["ant-2"]), forecast)
    assert forecast.open_swaps("ant-1", NOW) and forecast.open_swaps("ant-2", NOW) == []


# --- starting up ------------------------------------------------------------------------------


class Network:
    """urlopen for the startup check: each path answers with its queued replies in turn, an int
    being that HTTP status, an exception raised as is, anything else a 200 JSON body."""

    def __init__(self, replies: dict[str, list[Any]] | None = None) -> None:
        self.replies = replies or {}
        self.paths: list[str] = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        self.paths.append(path)
        reply = self.replies[path].pop(0) if len(self.replies[path]) > 1 else self.replies[path][0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            raise urllib.error.HTTPError(req.full_url, reply, "", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        return io.BytesIO(json.dumps(reply).encode())


CONFIG = {"document": DOCUMENT}
CONFIG_PATH = "topology/config"
TOKEN_PATH = "api/v1/objects?prefix=constellation-decisions-&maxPageSize=1"
LISTED = {"path_metadatas": []}


def started(monkeypatch, network: Network, without: tuple[str, ...] = ()) -> Any:
    module = load(monkeypatch, without)
    monkeypatch.setattr(module.urllib.request, "urlopen", network)
    monkeypatch.setattr(module.time, "sleep", lambda s: network.paths.append(f"slept {s}"))
    return module


@pytest.mark.parametrize("name", ["CONSTELLATION_API_KEY", "LATTICE_URL", "LATTICE_TOKEN"])
def test_a_missing_variable_stops_with_one_line_naming_it(monkeypatch, capsys, name):
    module = started(monkeypatch, Network(), without=(name,))
    with pytest.raises(SystemExit) as stopped:
        module.ready()
    assert stopped.value.code == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert name in line


def test_unset_decision_settings_take_their_defaults_and_the_start_log_says_so(monkeypatch):
    module = started(monkeypatch, Network(), without=("MIN_P10_GB", "MAX_FORECAST_AGE_S"))
    assert (module.MIN_P10_GB, module.MAX_FORECAST_AGE_S) == (0, 21600)
    assert module.settings() == "MIN_P10_GB=0 (default), MAX_FORECAST_AGE_S=21600 (default)"
    module = started(monkeypatch, Network(), without=("MAX_FORECAST_AGE_S",))
    assert module.settings() == "MIN_P10_GB=5, MAX_FORECAST_AGE_S=21600 (default)"


def test_state_lives_in_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_FILE")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    module = started(monkeypatch, Network())
    assert module.CURSOR_FILE == f"{tmp_path}/lattice-bridge-cursor.json"
    assert module.HEARTBEAT == f"{tmp_path}/heartbeat"


def test_outside_docker_state_is_in_the_users_state_directory_not_tmp(monkeypatch, tmp_path):
    """/tmp is cleared on reboot, which would make the next start a first start."""
    monkeypatch.delenv("CURSOR_FILE")
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    module = started(monkeypatch, Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [LISTED]}))
    assert module.STATE_DIR == f"{tmp_path}/xdg/constellation"
    assert module.ready() == DOCUMENT
    assert os.path.isdir(module.STATE_DIR)
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    module = started(monkeypatch, Network())
    assert module.CURSOR_FILE == f"{tmp_path}/.local/state/constellation/lattice-bridge-cursor.json"


@pytest.mark.parametrize("name", ["MIN_P10_GB", "MAX_FORECAST_AGE_S"])
@pytest.mark.parametrize("value", ["five", "-1", "nan"])
def test_a_decision_setting_that_is_not_a_number_stops_rather_than_defaulting(
    monkeypatch, capsys, name, value
):
    """A value the operator set that is not one is refused, never replaced by the default."""
    monkeypatch.setitem(ENV, name, value)
    module = started(monkeypatch, Network())
    with pytest.raises(SystemExit) as stopped:
        module.ready()
    assert stopped.value.code == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert name in line and "number" in line


@pytest.mark.parametrize("mode", ["under a file", "read-only"])
def test_a_state_dir_it_cannot_write_stops_at_start(monkeypatch, capsys, tmp_path, mode):
    """Otherwise every run is re-announced after a restart, as nothing it applied was kept."""
    state = tmp_path / "state"
    if mode == "under a file":
        (tmp_path / "file").touch()
        state = tmp_path / "file" / "state"
    if mode == "read-only":
        if os.geteuid() == 0:
            pytest.skip("root writes a read-only directory")
        state.mkdir(mode=0o500)
    monkeypatch.setenv("STATE_DIR", str(state))
    with pytest.raises(SystemExit) as stopped:
        started(monkeypatch, Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [LISTED]})).ready()
    assert stopped.value.code == 2
    (line,) = capsys.readouterr().err.splitlines()
    assert f"STATE_DIR {state} is not a directory this bridge can write" in line


def test_the_api_url_defaults_to_constellations(monkeypatch):
    module = started(monkeypatch, Network(), without=("CONSTELLATION_API_URL",))
    assert module.API == "https://api.constellation.space"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_api_key_at_start_is_said_once_and_asked_again_after_a_long_wait(
    monkeypatch, capsys, status
):
    """Exiting would have the restart policy hammer the API into an address-wide lockout."""
    network = Network({CONFIG_PATH: [status, status, CONFIG], TOKEN_PATH: [LISTED]})
    module = started(monkeypatch, network)
    assert module.ready() == DOCUMENT
    assert [p for p in network.paths if p.startswith("slept")] == ["slept 900", "slept 900"]
    (line,) = capsys.readouterr().err.splitlines()
    assert "CONSTELLATION_API_KEY" in line and "predictions:read and config:read" in line


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_lattice_token_stops_naming_both_tokens(monkeypatch, capsys, status):
    network = Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [status]})
    module = started(monkeypatch, network)
    with pytest.raises(SystemExit) as stopped:
        module.ready()
    assert stopped.value.code == 2
    assert (
        capsys.readouterr()
        .err.strip()
        .endswith(
            "Lattice rejected the token (LATTICE_TOKEN; on Lattice Sandboxes also"
            " LATTICE_SANDBOX_TOKEN, which expire with the environment)"
        )
    )


def test_outages_on_either_side_are_waited_out_with_growing_backoff(monkeypatch):
    down = urllib.error.URLError("connection refused")
    network = Network({CONFIG_PATH: [503, down, CONFIG], TOKEN_PATH: [502, 429, LISTED]})
    module = started(monkeypatch, network)
    assert module.ready() == DOCUMENT
    sleeps = [float(p.split()[1]) for p in network.paths if p.startswith("slept")]
    assert len(sleeps) == 4 and sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1]


def test_a_lattice_that_lists_objects_is_the_right_url_with_an_accepted_token(monkeypatch):
    network = Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [LISTED]})
    assert started(monkeypatch, network).ready() == DOCUMENT


def test_a_404_from_lattice_is_a_wrong_url_and_stops(monkeypatch, capsys):
    network = Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [404]})
    with pytest.raises(SystemExit) as stopped:
        started(monkeypatch, network).ready()
    assert stopped.value.code == 2
    assert "LATTICE_URL" in capsys.readouterr().err.strip().splitlines()[-1]


@pytest.mark.parametrize("status", [400, 405])
def test_any_other_refusal_from_lattice_stops_rather_than_passing(monkeypatch, capsys, status):
    network = Network({CONFIG_PATH: [CONFIG], TOKEN_PATH: [status]})
    with pytest.raises(SystemExit) as stopped:
        started(monkeypatch, network).ready()
    assert stopped.value.code == 2
    assert f"HTTP {status}" in capsys.readouterr().err


def test_each_successful_poll_touches_the_heartbeat(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    module = load(monkeypatch)
    fake = FakeLattice()
    fake.replies["https://api.test/predictions/feed"] = {"entries": [], "next": ""}
    monkeypatch.setattr(module, "call", lambda m, url, *a, **k: fake(m, url.split("?")[0], *a, **k))
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)

    def after_one_poll(_):
        raise SystemExit

    monkeypatch.setattr(module.time, "sleep", after_one_poll)
    with pytest.raises(SystemExit):
        module.main()
    assert heartbeat.exists()

    heartbeat.unlink()
    fake.fail["https://api.test/predictions/feed"] = "down"
    with pytest.raises(SystemExit):
        module.main()
    assert not heartbeat.exists()


def test_a_poll_whose_runs_cannot_be_applied_leaves_the_heartbeat_stale(
    monkeypatch, tmp_path, cursor_file
):
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    cursor_file.write_text(json.dumps({"cursor": "c#run_0", "forecast": None}))
    module = load(monkeypatch)
    fake = FakeLattice()
    fake.replies["https://api.test/predictions/feed"] = {
        "entries": [entry([item(12.0)])],
        "next": "c#run_1",
    }
    monkeypatch.setattr(module, "call", lambda m, url, *a, **k: fake(m, url.split("?")[0], *a, **k))
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)

    def stuck(*_):
        raise RuntimeError("this run cannot be applied")

    monkeypatch.setattr(module, "deliver", stuck)
    monkeypatch.setattr(module.time, "sleep", lambda _: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        module.main()
    assert not heartbeat.exists()


def publishing_to(monkeypatch, tmp_path, status: int) -> tuple[Any, Path]:
    """A bridge with a run to publish, whose Lattice answers every entity PUT with `status`."""
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setitem(os.environ, "STATE_DIR", str(tmp_path))
    (tmp_path / "cursor.json").write_text(json.dumps({"cursor": cursor_of(0), "forecast": None}))
    module = load(monkeypatch)
    feed = iter([[entry([item(12.0)], cursor=cursor_of(1))], []])

    def call(method, url, body=None, headers=None, raw=None):
        if url.startswith("https://api.test/predictions/feed?"):
            return {"entries": next(feed, []), "next": cursor_of(1)}
        if url.endswith("/entities"):
            raise module.HTTPFailure(f"PUT /api/v1/entities: HTTP {status}", status)
        return None

    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda _: (_ for _ in ()).throw(SystemExit))
    return module, heartbeat


def test_a_poll_whose_publish_lattice_refuses_leaves_the_heartbeat_stale(monkeypatch, tmp_path):
    module, heartbeat = publishing_to(monkeypatch, tmp_path, 503)
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code is None and not heartbeat.exists()


@pytest.mark.parametrize("status", [401, 403])
def test_a_lattice_token_rejected_while_running_stops_naming_both_tokens(
    monkeypatch, capsys, tmp_path, status
):
    module, _ = publishing_to(monkeypatch, tmp_path, status)
    with pytest.raises(SystemExit) as stopped:
        module.main()
    assert stopped.value.code == 2
    assert capsys.readouterr().err.strip().endswith(module.TOKEN_REJECTED)


def malformed_items() -> list[dict]:
    no_antenna = item(9.0, sat="sat-2")
    del no_antenna["entity"]["antenna_id"]
    bad_time = {**item(9.0, sat="sat-3"), "until": "soon"}
    return [no_antenna, bad_time]


def test_a_malformed_item_is_skipped_and_counted_and_the_rest_of_its_run_kept(bridge, capsys):
    module, _ = bridge
    forecast = module.Forecast()
    used = module.deliver(entry([*malformed_items(), item(12.0)]), forecast)
    assert used == (1, 0, 2, 0)
    assert forecast.find("ant-1", "sat-1", None, NOW) is not None
    assert capsys.readouterr().err.count("it is malformed") == 2
    forecast.saved(NOW)


def test_a_malformed_item_never_holds_up_the_runs_after_it(monkeypatch, cursor_file):
    feed = [
        entry([item(12.0)], cursor=cursor_of(1)),
        entry([*malformed_items(), item(12.0)], cursor=cursor_of(2)),
        entry([item(3.0, ant="ant-2")], cursor=cursor_of(3)),
    ]
    cursor_file.write_text(json.dumps({"cursor": cursor_of(1), "forecast": None}))
    _, lattice, afters = ran(monkeypatch, feed)
    assert reported(lattice) == ["run_2", "run_3"] and afters[-1] == cursor_of(3)
    (malformed,) = [
        b["applied"] for _, p, b, _ in lattice.calls
        if p.endswith("/feed/deliveries") and b["run_id"] == "run_2"
    ]
    assert malformed["malformed"] == 2


# --- where the feed is read from --------------------------------------------------------------


def cursor_of(n: int) -> str:
    return f"20261106T00000{n}.000000#run_{n}"


def ran(monkeypatch, feed: list[dict], polls: int = 1, oldest: str | None = None) -> tuple:
    """Runs a freshly started bridge against `feed` until it has polled `polls` times with nothing
    new. Returns the module, its Lattice and each feed query's `after`."""
    module = load(monkeypatch)
    lattice, afters = FakeLattice(), []

    def call(method, url, body=None, headers=None, raw=None):
        if url.startswith("https://api.test/predictions/feed?"):
            query = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
            if query.get("from") == "latest":
                return {"entries": [], "next": api.start(feed), "oldest": oldest}
            after = query.get("after", "")
            afters.append(after)
            entries = [e for e in feed if e["cursor"] > after][: int(query["limit"])]
            nxt = entries[-1]["cursor"] if entries else after
            return {"entries": entries, "next": nxt, "oldest": oldest}
        return lattice(method, url, body, headers, raw)

    sleeps = iter(range(polls - 1))
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", lambda s: next(sleeps))
    with pytest.raises(StopIteration):
        module.main()
    return module, lattice, afters


def reported(lattice: FakeLattice) -> list[str]:
    return [b["run_id"] for _, p, b, _ in lattice.calls if p.endswith("/feed/deliveries")]


def test_a_first_start_applies_only_the_newest_run_for_this_connection(monkeypatch):
    feed = [
        entry([item(12.0)], cursor=cursor_of(1)),
        entry([item(12.0)], cursor=cursor_of(2)),
        # A run for other connections only: the feed serves it to this one with no resources.
        {"cursor": cursor_of(3), "otlp": {"resourceLogs": []}},
    ]
    _, lattice, _ = ran(monkeypatch, feed)
    assert reported(lattice) == ["run_2"]


def test_a_run_with_nothing_lattice_shows_is_still_reported(monkeypatch, cursor_file):
    rfi = {
        "entity": {"type": "link", "id": "ant-1:sat-1", "antenna_id": "ant-1"},
        "target_at": NOW.isoformat(),
        "until": (NOW + timedelta(hours=1)).isoformat(),
        "value": {"p10": 2.0, "p50": 2.0, "p90": 2.0, "lock_loss_risk": 0.1},
        "action": {"verb": "reroute", "from": "ant-1:sat-1", "mode": "suggest"},
    }
    feed = [
        entry([item(12.0)], cursor=cursor_of(1)),
        # A swap run advising nothing, then a run of links Lattice draws nothing for.
        entry([], cursor=cursor_of(2)),
        entry([rfi], cursor=cursor_of(3)),
    ]
    cursor_file.write_text(json.dumps({"cursor": cursor_of(0), "forecast": None}))
    _, lattice, _ = ran(monkeypatch, feed)
    assert reported(lattice) == ["run_1", "run_2", "run_3"]


def test_a_restart_resumes_after_the_last_cursor_with_the_forecast_it_held(monkeypatch):
    feed = [entry([item(12.0)], cursor=cursor_of(1)), entry([item(12.0)], cursor=cursor_of(2))]
    _, first, _ = ran(monkeypatch, feed)
    assert reported(first) == ["run_2"]
    feed.append(entry([item(3.0, ant="ant-2")], cursor=cursor_of(3)))
    module, again, afters = ran(monkeypatch, feed)
    assert afters[0] == cursor_of(2) and reported(again) == ["run_3"]
    # The pass run 2 forecast for ant-1 is still published after the restart.
    assert {e["entityId"] for e in again.entities()} >= {
        module.entity_id("ant-1"),
        module.entity_id("ant-2"),
    }


def test_a_pass_dropped_after_a_restart_is_still_withdrawn(monkeypatch):
    first, second = item(9.0, start=timedelta(hours=1)), item(9.0, start=timedelta(hours=2))
    feed = [entry([first, second], cursor=cursor_of(1))]
    ran(monkeypatch, feed)
    feed.append(entry([second], cursor=cursor_of(2)))
    module, lattice, _ = ran(monkeypatch, feed)
    withdrawn = [e["entityId"] for e in lattice.entities() if e["isLive"] is False]
    assert withdrawn == [module.pass_entity_id(first["entity"]["id"])]


def test_the_network_config_is_read_again_so_a_new_antenna_needs_no_restart(
    monkeypatch, cursor_file
):
    cursor_file.write_text(json.dumps({"cursor": cursor_of(0), "forecast": None}))
    module = load(monkeypatch)
    grown = json.loads(json.dumps(DOCUMENT))
    grown["ground_stations"]["gs-1"]["antennas"]["ant-3"] = {"min_elevation_deg": 10}
    fake = FakeLattice()
    fake.replies["https://api.test/predictions/feed"] = {"entries": [], "next": cursor_of(0)}
    fake.replies["https://api.test/topology/config"] = {"document": grown}
    clock, held = [0.0], []
    monkeypatch.setattr(module, "call", lambda m, url, *a, **k: fake(m, url.split("?")[0], *a, **k))
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda tasks, agents, forecast: held.append(agents))
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    def sleep(seconds):
        clock[0] += 300
        if clock[0] > 900:
            raise SystemExit

    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        module.main()
    configs = [p for _, p, _, _ in fake.calls if p.endswith("/topology/config")]
    assert len(configs) == 1
    # The task thread's antennas, updated in place.
    assert set(held[0].values()) == {"ant-1", "ant-2", "ant-3"}


def test_the_task_stream_listens_again_when_the_antennas_change(bridge, monkeypatch):
    module, _ = bridge
    agents, selectors = {"a1": "ant-1"}, []

    class Stream(io.BytesIO):
        def __iter__(self):
            yield b"data: {}\n"
            agents["a3"] = "ant-3"
            yield b"data: {}\n"
            pytest.fail("kept listening on the old antennas")

    def urlopen(req, timeout=None):
        selectors.append(json.loads(req.data)["agentSelector"]["entityIds"])
        if len(selectors) > 1:
            raise SystemExit
        return Stream()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(SystemExit):
        module.stream(agents, queue.Queue())
    assert selectors == [["a1"], ["a1", "a3"]]


def test_a_cursor_older_than_the_feed_holds_says_runs_expired_and_reads_on(
    monkeypatch, capsys, cursor_file
):
    cursor_file.write_text(json.dumps({"cursor": cursor_of(1), "forecast": None}))
    feed = [entry([item(12.0)], cursor=cursor_of(5))]
    _, lattice, afters = ran(monkeypatch, feed, oldest=cursor_of(5))
    assert afters[0] == cursor_of(1) and reported(lattice) == ["run_5"]
    err = capsys.readouterr().err
    assert "expired" in err and cursor_of(1) in err and cursor_of(5) in err


PLAN = json.dumps(
    {"detail": {"error": "plan_required", "plan": "enterprise", "feature": "output:lattice"}}
).encode()


LOCKOUT = json.dumps({"error": "auth_lockout", "retry_after_seconds": 1200}).encode()


@pytest.mark.parametrize("refused", ["/predictions/feed/checkin", "/predictions/feed?"])
@pytest.mark.parametrize(
    "status, answer, says, wait",
    [
        (403, PLAN, "plan does not include output:lattice", 900),
        (401, PLAN, "401", 900),
        (429, LOCKOUT, "locked out this address", 1200),
        (429, b'{"error": "rate_limited"}', "HTTP 429 rate_limited", 60),
        (402, b"{}", "HTTP 402", 3600),
    ],
)
def test_a_refused_key_or_a_lockout_is_said_once_and_waited_out_never_hammered(
    monkeypatch, capsys, cursor_file, refused, status, answer, says, wait
):
    """A 401 or 403 does not clear by asking again, and a 402 or 429 asks the bridge to wait:
    it says why and how to fix it in one line and asks again only after a long wait."""
    cursor_file.write_text(json.dumps({"cursor": cursor_of(0), "forecast": None}))
    module = load(monkeypatch)
    lattice, asked, slept = FakeLattice(), [], []

    def call(method, url, body=None, headers=None, raw=None):
        if url.startswith("https://api.test"):
            asked.append(url)
            if refused in url and len(slept) < 3:
                raise module.HTTPFailure(f"{method} {url}: HTTP {status}", status, answer)
            return {"entries": [], "next": ""} if "/predictions/feed?" in url else None
        return lattice(method, url, body, headers, raw)

    def sleep(seconds):
        slept.append(seconds)
        if seconds == module.POLL_S:
            raise SystemExit

    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        module.main()
    assert slept == [wait, wait, wait, module.POLL_S]
    assert len([u for u in asked if refused in u]) == 4
    (line,) = capsys.readouterr().err.splitlines()
    assert says in line


def test_a_resource_tagged_for_another_system_is_refused_whatever_its_output_says(bridge, capsys):
    """An API before the os fix tagged a mapping-less output yamcs; the tag decides."""
    module, _ = bridge
    forecast = module.Forecast()
    run = entry([item(12.0)])
    assert module.deliver({**run, "otlp": api.tagged(run["otlp"], "yamcs")}, forecast) == (0, 1, 0, 0)
    assert forecast.antennas() == set()
    assert "it is a yamcs output, not a Lattice one" in capsys.readouterr().err


def approved_of(
    healthy: dict, deadline: timedelta = timedelta(minutes=30), to: str | None = "ant-2"
) -> dict:
    """An approved swap: as swap_of, approved in Constellation."""
    s = swap_of(healthy, deadline)
    action = {**s["action"], "mode": "approve"}
    if to is None:
        action |= {"to": None, "to_antenna_id": None, "to_at": None}
    return {**s, "action": action, "action_status": "approved"}


def held_of(healthy: dict, status: str = "pending") -> dict:
    """A swap held for approval (or rejected): no action is sent, only its status."""
    return {**{k: v for k, v in swap_of(healthy).items() if k != "action"}, "action_status": status}


def test_a_held_swap_run_leaves_the_antennas_other_passes_taskable(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    at_risk, other = item(9.0), item(12.0, sat="sat-2", start=timedelta(hours=2))
    module.deliver(entry([at_risk, other]), forecast)
    module.deliver(entry([held_of(at_risk)], workflow="swaps", cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-2"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW"
    assert module.gb_of(forecast.find("ant-1", "sat-1", None, NOW))["p10"] == 9.0


def test_a_held_swap_run_of_the_same_workflow_drops_only_the_pass_it_holds(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    at_risk, other = item(9.0), item(12.0, sat="sat-2", start=timedelta(hours=2))
    module.deliver(entry([at_risk, other]), forecast)
    module.deliver(entry([held_of(at_risk)], cursor="c#run_2"), forecast)
    assert forecast.find("ant-1", "sat-1", None, NOW) is None
    assert forecast.find("ant-1", "sat-2", None, NOW) is not None


def test_a_pass_run_with_no_passes_clears_its_workflows_forecast(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(9.0)], workflow="a"), forecast)
    module.deliver(entry([item(7.0, ant="ant-2")], workflow="b"), forecast)
    run = {"workflow": "a", "run_id": "run_2", "template": "pass-yield"}
    lattice = output("lattice-bridge", {"system": "lattice"})
    nothing = {"cursor": "c#run_2", "otlp": to_otlp("t", [], NOW, run, lattice)}
    module.deliver(nothing, forecast)
    assert forecast.find("ant-1", "sat-1", None, NOW) is None
    assert forecast.find("ant-2", "sat-1", None, NOW) is not None


def test_a_rejected_swap_is_neither_advice_nor_a_forecast(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    rejected = held_of(healthy, "rejected")
    module.deliver(entry([rejected], workflow="swaps", cursor="c#run_2"), forecast)
    assert forecast.swaps == {} and forecast.held == {}
    assert forecast.find("ant-1", "sat-1", None, NOW)["provenance"]["workflow"] == "w"


def test_a_task_on_a_pass_with_an_approved_swap_is_denied_and_reserves_nothing(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([approved_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    agent = module.entity_id("ant-1")
    decision = module.handle(task({"satelliteId": "sat-1"}, agent), {agent: "ant-1"}, forecast)
    assert decision["decision"] == "DENY"
    assert decision["diagnostic"].startswith("SWAP_APPROVED: ")
    assert "book sat-1 on ant-2" in decision["diagnostic"]
    done = lattice.statuses()[-1]
    assert done["status"] == "STATUS_DONE_NOT_OK" and done["result"]["value"]["reserved"] is False


def test_an_approved_swap_without_a_backup_still_denies_and_says_so(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy]), forecast)
    alone = approved_of(healthy, to=None)
    module.deliver(entry([alone], workflow="swaps", cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "DENY" and "no better pass within 6 h" in decision["diagnostic"]


def test_with_advisories_off_an_approved_swap_does_not_decide_the_task(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy], advisories=False), forecast)
    module.deliver(entry([approved_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW"


def test_advice_still_rides_along_and_allows(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([swap_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW" and decision["advisory"].startswith("rain-fade risk: ")


def test_an_approved_swap_is_a_warning_alert_and_held_ones_are_counted(bridge):
    module, lattice = bridge
    forecast = module.Forecast()
    first, second = item(9.0), item(9.0, start=timedelta(hours=2))
    module.deliver(entry([first, second]), forecast)
    swaps = [approved_of(first), held_of(second)]
    module.deliver(entry(swaps, workflow="swaps", cursor="c#run_2"), forecast)
    module.publish([STATION], forecast, {})
    antenna = served(module, lattice)
    (alert,) = antenna["health"]["activeAlerts"]
    assert alert["alertCode"] == "CONSTELLATION_SWAP_APPROVED"
    assert alert["level"] == "ALERT_LEVEL_WARNING"
    assert alert["activeConditions"][0]["conditionCode"] == "SWAP_APPROVED"
    names = [c["name"] for c in antenna["health"]["components"]]
    assert "1 rain-fade swap awaiting approval in Constellation" in names
    rain = [e for e in lattice.entities() if e["ontology"]["platformType"] == "Forecast Rain Fade"]
    assert rain and "swap approved" in rain[0]["aliases"]["name"]


def test_an_approved_swap_sent_again_in_later_runs_and_after_a_restart_is_one_alert(bridge):
    """Lattice alerts are the antenna's state, replaced on each publish, not events: the API
    sending an approved swap again (on approval, then in each later swap run) keeps one alert."""
    module, lattice = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    for cursor in ("c#run_2", "c#run_3"):
        module.deliver(entry([approved_of(healthy)], workflow="swaps", cursor=cursor), forecast)
    restarted = module.Forecast()
    restarted.restore(forecast.saved(NOW))
    module.deliver(entry([approved_of(healthy)], workflow="swaps", cursor="c#run_4"), restarted)
    module.publish([STATION], restarted, {})
    codes = [a["alertCode"] for a in served(module, lattice)["health"]["activeAlerts"]]
    assert codes == ["CONSTELLATION_SWAP_APPROVED"]


def test_approving_a_held_swap_moves_it_from_held_to_approved(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([held_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    assert len(forecast.held_swaps("ant-1", NOW)) == 1
    module.deliver(entry([approved_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    assert forecast.held_swaps("ant-1", NOW) == []
    states = [module.results.action_state(s) for s in forecast.open_swaps("ant-1", NOW)]
    assert states == ["approved"]


def test_held_swaps_survive_a_restart_and_an_older_state_restores_with_none(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(9.0)
    module.deliver(entry([healthy]), forecast)
    module.deliver(entry([held_of(healthy)], workflow="swaps", cursor="c#run_2"), forecast)
    saved = forecast.saved(NOW)
    again = module.Forecast()
    again.restore(saved)
    assert len(again.held_swaps("ant-1", NOW)) == 1
    older = module.Forecast()
    older.restore({k: v for k, v in saved.items() if k != "held"})
    assert older.held == {}


def test_an_approved_swap_past_its_cancel_deadline_no_longer_decides_the_task(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy]), forecast)
    late = approved_of(healthy, deadline=timedelta(minutes=-5))
    module.deliver(entry([late], workflow="swaps", cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW"


def test_a_held_action_sent_with_its_body_is_held_not_advice(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.4)
    module.deliver(entry([healthy]), forecast)
    held = {**swap_of(healthy), "action_status": "pending"}
    module.deliver(entry([held], workflow="swaps", cursor="c#run_2"), forecast)
    assert forecast.swaps == {} and len(forecast.held_swaps("ant-1", NOW)) == 1
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW" and "advisory" not in decision


def test_a_new_run_replaces_its_workflows_whole_pass_set(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(9.0), item(9.0, ant="ant-2")]), forecast)
    module.deliver(entry([item(9.0, ant="ant-2")], cursor="c#run_2"), forecast)
    assert forecast.find("ant-1", "sat-1", None, NOW) is None
    assert forecast.find("ant-2", "sat-1", None, NOW) is not None


def test_a_pass_that_became_held_is_not_allowed_on_its_old_value(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    at_risk = item(9.0)
    module.deliver(entry([at_risk]), forecast)
    module.deliver(entry([held_of(at_risk)], cursor="c#run_2"), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "DENY"
    assert len(forecast.held) == 1


def test_a_run_replaces_only_its_own_workflows_passes_on_a_shared_antenna(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(9.0)], workflow="a"), forecast)
    module.deliver(entry([item(12.0, sat="sat-2")], workflow="b", cursor="c#run_2"), forecast)
    module.deliver(entry([item(10.0)], workflow="a", cursor="c#run_3"), forecast)
    assert module.gb_of(forecast.find("ant-1", "sat-1", None, NOW))["p10"] == 10.0
    assert forecast.find("ant-1", "sat-2", None, NOW)["provenance"]["workflow"] == "b"
    module.deliver(entry([held_of(item(10.0))], workflow="a", cursor="c#run_4"), forecast)
    assert forecast.find("ant-1", "sat-1", None, NOW) is None
    assert forecast.find("ant-1", "sat-2", None, NOW) is not None


def test_two_workflows_on_one_antenna_do_not_flip_each_others_tasks_to_deny(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    module.deliver(entry([item(12.0)], workflow="a"), forecast)
    module.deliver(entry([item(12.0, sat="sat-2")], workflow="b", cursor="c#run_2"), forecast)
    for satellite in ("sat-1", "sat-2"):
        decision = module.decide(task({"satelliteId": satellite}, "x"), "ant-1", forecast, NOW)
        assert decision["decision"] == "ALLOW", decision["diagnostic"]


def test_expected_gb_is_read_from_the_item_whatever_node_id_its_ledger_uses(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    copied = item(12.0)
    copied["explain"] = {"gb_node": copied["explain"].pop("expected_gb"), **copied["explain"]}
    module.deliver(entry([copied]), forecast)
    decision = module.decide(task({"satelliteId": "sat-1"}, "x"), "ant-1", forecast, NOW)
    assert decision["decision"] == "ALLOW" and decision["expected_gb"]["p10"] == 12.0


def test_a_failed_first_check_in_is_retried_with_backoff_then_repeated(monkeypatch, cursor_file):
    module = load(monkeypatch)
    fake = FakeLattice()
    fake.replies["https://api.test/predictions/feed"] = {"entries": [], "next": "c#run_0"}
    cursor_file.write_text(json.dumps({"cursor": "c#run_0", "forecast": None}))
    checked: list[float] = []
    clock = [0.0]

    def check_in():
        checked.append(clock[0])
        return len(checked) > 2

    def sleep(seconds):
        clock[0] += 100
        if clock[0] > 1000:
            raise SystemExit

    monkeypatch.setattr(module, "call", lambda m, url, *a, **k: fake(m, url.split("?")[0], *a, **k))
    monkeypatch.setattr(module, "check_in", check_in)
    monkeypatch.setattr(module, "ready", lambda: DOCUMENT)
    monkeypatch.setattr(module, "stream", lambda *a: None)
    monkeypatch.setattr(module, "work", lambda *a: None)
    monkeypatch.setattr(module, "publish", lambda *a: 0)
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module, "POLL_S", 50)
    with pytest.raises(SystemExit):
        module.main()
    # Failed at 0 and retried after 50 s (the poll at 100), failed again and retried after
    # 100 s (200), then every 300 s once it succeeds.
    assert checked == [0.0, 100.0, 200.0, 500.0, 800.0]


def test_a_run_reports_the_approved_swaps_it_enforces_as_acted(bridge):
    module, _ = bridge
    forecast = module.Forecast()
    healthy = item(12.0)
    closed = approved_of(item(9.0, ant="ant-2"), deadline=timedelta(minutes=-1))
    assert module.deliver(entry([approved_of(healthy), closed, swap_of(healthy)]), forecast)[3] == 1


def test_a_standing_approval_is_acted_once_across_runs_and_restarts_and_a_new_one_again(bridge):
    """Each run carries a standing approval again: the platform counts it carried out once per
    decision, as the Yamcs and custom bridges report it."""
    module, _ = bridge
    forecast = module.Forecast()
    approval = {**approved_of(item(12.0)), "decision_id": "d1"}
    assert module.deliver(entry([approval]), forecast)[3] == 1
    assert module.deliver(entry([approval], cursor="c#run_2"), forecast)[3] == 0
    restarted = module.Forecast()
    restarted.restore(forecast.saved(NOW))
    assert module.deliver(entry([approval], cursor="c#run_3"), restarted)[3] == 0
    again = {**approval, "decision_id": "d2"}
    assert module.deliver(entry([again], cursor="c#run_4"), restarted)[3] == 1


@pytest.mark.parametrize(
    "url, stops",
    [("http://example.com", True), ("http://localhost:8000", False), ("https://api.test", False)],
)
def test_plain_http_to_another_host_stops_at_start_before_the_key_is_sent(
    monkeypatch, capsys, url, stops
):
    monkeypatch.setitem(ENV, "CONSTELLATION_API_URL", url)
    module = load(monkeypatch)

    def network(req, timeout=None):
        raise SystemExit(f"sent to {req.full_url}")

    monkeypatch.setattr(module.urllib.request, "urlopen", network)
    with pytest.raises(SystemExit) as stopped:
        module.ready()
    if stops:
        assert stopped.value.code == 2
        (line,) = capsys.readouterr().err.splitlines()
        assert "not sending the API key to http://example.com" in line and "https" in line
    else:
        assert str(stopped.value.code).startswith("sent to")
