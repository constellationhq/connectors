"""Constellation workflow results into Anduril Lattice, and Lattice tasks gated by them.

Constellation is the judgment; Lattice is the taskable antenna and the common operating picture.

    show  Each ground station is a Lattice asset entity on the map grouping its antennas, and each
          antenna an asset entity without a location of its own (restart-stable ids), republished
          every few minutes. An antenna's status rows are its next forecast passes, MODCOD plans
          and daily capacity; it carries a rain-fade alert, and its station a hazard, while a swap
          is advised for one of them; each listed pass is also its own short-lived line along its
          ground track that expires at LOS. Satellites with an element set carry their orbit.
          Everything is labelled a forecast, never a scheduled contact.
    act   Each antenna is a taskable agent for ReservePass. When an operator tasks one, the bridge
          looks up Constellation's forecast for that satellite on that antenna and:
            ALLOW  expected GB p10 at or above the floor -> STATUS_DONE_OK, outcome
                   ALLOWED_NOT_RESERVED: the bridge decides, it books nothing
            DENY   below the floor, no forecast, or a forecast made more than MAX_FORECAST_AGE_S
                   ago                                   -> STATUS_DONE_NOT_OK, ERROR_CODE_REJECTED
          Every decision is uploaded to Lattice Objects with its provenance (workflow version,
          run, when the run was made, p10/p50/p90, the floor used, the ledger). The bridge never
          re-tasks: after DONE_NOT_OK the Lattice operator decides.

The floor, how many passes to list, whether to attach rain-fade advice and publish pass entities,
and which antennas to cover are the workflow's Lattice mapping, set on its feed output node and
read per run from the feed; MIN_P10_GB (default 0) is only the floor a decision records for an
antenna no workflow has forecast yet. MAX_FORECAST_AGE_S defaults to 21600 (6 hours, one
weather-model cycle). The start log says which are defaults; a value that is not a number at
least 0 stops the bridge at start (exit 2).

It pulls the Constellation feed outbound as its own connection (CONSUMER, default
"lattice-bridge"), applies only the Lattice outputs addressed to that connection (any other
system's is refused and reported), checks in, and reports what it published per run, like the
Yamcs bridge. It keeps the last cursor applied and the forecast it holds in CURSOR_FILE, so a
restart resumes there; a first start (no state) applies only the newest run for this connection,
never the history. The image keeps it in STATE_DIR, a volume. Items are told apart by their fields,
not by the template that made them: a pass with expected GB is a forecast, and an item whose
action is a swap is rain-fade advice for its pass.
Lattice credentials stay here: the environment token (LATTICE_TOKEN) and, for Lattice
Sandboxes, the account token (LATTICE_SANDBOX_TOKEN).

Stdlib only. REST paths are Lattice's (developer.anduril.com/reference/rest).
"""

import json
import math
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from constellation_space import results

VERSION = "0.7.1"
API = (os.environ.get("CONSTELLATION_API_URL") or "https://api.constellation.space").rstrip("/")
API_KEY = os.environ.get("CONSTELLATION_API_KEY", "").strip()
LATTICE = os.environ.get("LATTICE_URL", "").rstrip("/")
CONSUMER = os.environ.get("CONSUMER", "lattice-bridge")


def number(name: str, default: float) -> float | None:
    """A decision setting: a finite number, at least 0, or `default` when unset; None when it is
    set to anything else."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


# Checked by ready(). The p10 floor (GB) for an antenna no workflow has forecast yet: 0 accepts
# any forecast pass there until a workflow forecasts the antenna and its own floor wins.
MIN_P10_GB = number("MIN_P10_GB", 0)
# Checked by ready(). The oldest forecast (seconds since its run was made) a decision may use.
MAX_FORECAST_AGE_S = number("MAX_FORECAST_AGE_S", 21600)
POLL_S = int(os.environ.get("POLL_SECONDS", "15"))
# How long a key Constellation refused waits before it is tried again: the API's lockout window.
REFUSED_S = 900
# How often the network config is read again, so new stations and antennas appear without a restart.
CONFIG_S = 600
PUBLISH_S = 240
# Where the bridge keeps its state: the image sets it to its volume, /var/lib/constellation;
# outside Docker the user's state directory, never /tmp, which a reboot clears.
STATE_DIR = os.environ.get("STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "constellation"
)
# Touched after every feed poll whose runs were all applied and published to Lattice (or that had
# nothing due), for a container healthcheck: a bridge that stops making progress goes stale.
HEARTBEAT = f"{STATE_DIR}/heartbeat"
# The last feed cursor applied and the forecast held then, so a restart resumes there with the
# same forecast. Without it a start is a first start.
CURSOR_FILE = os.environ.get("CURSOR_FILE", f"{STATE_DIR}/lattice-bridge-cursor.json")
INTEGRATION = "constellation"
# The task type each antenna advertises. Lattice resolves only types it knows: by default the
# well-known Struct (fields satelliteId and, optionally, aos); once ReservePass is pushed to the
# Lattice Schema Registry (proto/), its type URL, which gets operators a form in the UI.
TASK_TYPE = os.environ.get("TASK_TYPE", "type.googleapis.com/google.protobuf.Struct")
NAMESPACE = uuid.UUID("2b0c3f4e-9a51-4c1d-8e7a-6f2d9b1c0a33")
# A task names a pass by satellite and, optionally, AOS; this is how far AOS may be off.
AOS_SLACK = timedelta(minutes=10)
# MIL-STD-2525C friendly ground unit, signal, tactical satellite: a satellite ground station.
GROUND_STATION_SIDC = "SFGPUUSRS------"
# The Lattice UI draws an icon only for the platform types it knows (AIRPLANE, ANIMAL, CAR,
# PERSON, RADAR, SATELLITE, SURFACE VESSEL, SUBMARINE, UAV, VEHICLE); anything else is a dot.
# RADAR is the one that reads as a dish on the ground.
GROUND_STATION_ICON = "RADAR"
# An antenna shows its soonest open rain-fade swaps; the rest are counted in a status row.
ALERTS_SHOWN = 2
# Constellation is a forecast layer in Lattice, never the customer's own assets: their stations,
# antennas and satellites are theirs. Stations, satellites and coverage circles are published only
# where Lattice has none of its own, e.g. an empty sandbox; each antenna's forecast is a service
# entity named as one, never the dish.
PUBLISH_STATIONS = os.environ.get("PUBLISH_STATIONS", "false").lower() == "true"
PUBLISH_SATELLITES = os.environ.get("PUBLISH_SATELLITES", "false").lower() == "true"
PUBLISH_COVERAGE = os.environ.get("PUBLISH_COVERAGE", "false").lower() == "true"
# Every forecast shape on the map is drawn in this one colour, so none passes for a real track.
FORECAST_COLOR = {"red": 155, "green": 93, "blue": 229, "alpha": 1}
# Days of capacity an antenna lists.
CAPACITY_DAYS = 2
# The radius drawn for a forecast rain cell at a station: where the fade is, not how far it reaches.
RAIN_RADIUS_M = 20_000.0
EARTH_RADIUS_KM = 6378.137
MU_KM3_S2 = 398600.4418
WGS84_E2 = 6.69437999014e-3
EARTH_RATE_RAD_S = 7.2921150e-5
# How often satellites' positions are republished: a LEO satellite crosses ~75 km in 10 s.
SATELLITE_S = 10
# The feed output mapping versions this bridge reads.
MAPPING_VERSIONS = [2]
# What an output without a Lattice mapping gets.
FALLBACK = {
    "min_p10_gb": MIN_P10_GB,
    "passes_listed": 3,
    "advisories": True,
    "pass_entities": True,
    "antennas": None,
}


def log(message: str) -> None:
    print(f"lattice-bridge: {message}", flush=True)


def warn(message: str) -> None:
    print(f"lattice-bridge: {message}", file=sys.stderr, flush=True)


class HTTPFailure(RuntimeError):
    def __init__(self, message: str, code: int, body: bytes = b"") -> None:
        super().__init__(message)
        self.code = code
        self.body = body


def call(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> Any:
    try:
        return results.http(method, url, body, headers, raw, timeout=330)
    except urllib.error.HTTPError as exc:
        # The body says why (Lattice: {"code":..,"message":..}); a bare status says nothing.
        answer = exc.read()[:500]
        detail = answer.decode(errors="replace")
        path = urllib.parse.urlsplit(url).path
        raise HTTPFailure(f"{method} {path}: HTTP {exc.code} {detail}", exc.code, answer) from None


def constellation(method: str, path: str, body: Any = None) -> Any:
    """A Constellation API call. A refused key or plan (401, 403) or a hold (402, 429: locked
    out, rate limited) is said once with the fix and asked again only after a long wait: exiting
    would have the restart policy hammer the API into locking out every key on this address."""
    said = ""
    while True:
        try:
            return call(method, f"{API}{path}", body, headers={"X-API-Key": API_KEY})
        except HTTPFailure as exc:
            wait: float = REFUSED_S
            why = results.refusal(exc.code, exc.body, "predictions:read and config:read", API)
            if not why and (held := results.held_off(exc.code, exc.body, API)):
                why, wait = held
            if not why:
                raise
            if why != said:
                warn(f"{why}. Asking again in {wait:.0f} s")
                said = why
            time.sleep(wait)


def lattice_headers() -> dict[str, str]:
    headers = {"Authorization": f"Bearer {os.environ['LATTICE_TOKEN'].strip()}"}
    if os.environ.get("LATTICE_SANDBOX_TOKEN"):
        headers["Anduril-Sandbox-Authorization"] = (
            f"Bearer {os.environ['LATTICE_SANDBOX_TOKEN'].strip()}"
        )
    return headers


def lattice(method: str, path: str, body: Any = None, raw: bytes | None = None) -> Any:
    headers = lattice_headers()
    if raw is not None:
        headers["Content-Type"] = "application/octet-stream"
    return call(method, f"{LATTICE}/api/v1{path}", body, headers, raw)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- what Constellation forecasts -----------------------------------------------------------



def gb_of(kept: dict[str, Any]) -> dict[str, float]:
    """The expected GB of a pass the forecast kept (it keeps only passes that have it)."""
    gb = results.expected_gb(kept)
    assert gb is not None
    return gb



def is_modcod_plan(item: dict[str, Any]) -> bool:
    return any("modcod" in (s.get("value") or {}) for s in item.get("series") or [])


def is_swap(item: dict[str, Any]) -> bool:
    """Swap advice, or a swap approved in Constellation: never one held or rejected, even when
    its action is sent with it."""
    return results.action_state(item) in ("advice", "approved") and (
        item.get("action") or {}
    ).get("verb") == "swap"


def is_forecast(item: dict[str, Any]) -> bool:
    """A pass still to be flown, priced, with no action of any kind (advice, approved, held or
    rejected) and no severity (an advisory, such as a rain-fade risk with no backup to swap to):
    not a past pass audited against a ledger, and not a modcod plan (a pass with a schedule over
    its steps)."""
    explain = item.get("explain") or {}
    return (
        results.action_state(item) is None
        and not item.get("severity")
        and not item.get("series")
        and "ledger" not in explain
        and "modcod_changes" not in explain
    )


class Forecast:
    """The newest priced passes per antenna with the mapping of the workflow they came from,
    and the open rain-fade swaps, from the feed."""

    def __init__(self) -> None:
        self.passes: dict[str, list[dict[str, Any]]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.swaps: dict[str, dict[str, Any]] = {}
        # Swaps held for approval in Constellation, by pass id: counted, never a forecast.
        self.held: dict[str, dict[str, Any]] = {}
        # Per antenna: daily capacity by day, and MODCOD plans by the pass they plan.
        self.capacity: dict[str, dict[str, dict[str, Any]]] = {}
        self.modcod: dict[str, dict[str, dict[str, Any]]] = {}
        # Approvals already reported carried out, by decision: each run enforces them again.
        self.acted: dict[str, str] = {}
        self.lock = threading.Lock()

    def take(
        self, items: list[dict[str, Any]], mapping: dict[str, Any], run: dict[str, Any]
    ) -> tuple[int, int]:
        """Keeps what one feed output of a run adds; returns how many results it used and how
        many it skipped as malformed. `run` names the workflow and run the items came from,
        kept on each as its provenance."""
        keep = mapping["antennas"]
        by_antenna: dict[str, list[dict[str, Any]]] = {}
        swaps: dict[str, dict[str, Any]] = {}
        held: dict[str, dict[str, Any]] = {}
        capacity: dict[str, dict[str, dict[str, Any]]] = {}
        modcod: dict[str, dict[str, dict[str, Any]]] = {}
        malformed = 0
        for n, item in enumerate(items):
            try:
                entity = item["entity"]
                antenna = entity["id"] if entity["type"] == "antenna" else entity.get("antenna_id")
                if keep is not None and antenna not in keep:
                    continue
                if entity["type"] == "antenna" and "p50" in (item.get("value") or {}):
                    when(item["until"])
                    capacity.setdefault(antenna, {})[item["target_at"][:10]] = item
                    continue
                if entity["type"] == "pass" and is_modcod_plan(item):
                    when(item["until"])
                    modcod.setdefault(antenna, {})[entity["id"]] = item
                    continue
                if entity["type"] != "pass" or results.expected_gb(item) is None:
                    continue
                # A kept pass is sorted and aged by its times: they must read as times.
                when(item["target_at"]), when(item["until"])
                kept = {
                    **item,
                    "provenance": {
                        "workflow": run.get("workflow"),
                        "run_id": run.get("run_id"),
                        "workflow_version": item.get("workflow_version"),
                        "made_at": run.get("made_at"),
                    },
                }
                if is_swap(item):
                    swaps[entity["id"]] = kept
                elif results.action_state(item) == "pending":
                    held[entity["id"]] = kept
                elif is_forecast(item):
                    by_antenna.setdefault(entity["antenna_id"], []).append(kept)
            except results.MALFORMED as exc:
                # One item is never worth the runs after it: skipped, counted, and logged.
                warn(f"skipped item {n} of run {run.get('run_id')}: it is malformed ({exc!r})")
                malformed += 1

        with self.lock:
            # Swap advice is its workflow's whole advice for its window on its antennas: it
            # replaces what that workflow advised or held there, and a swap run advising nothing
            # takes that down. Other workflows' advice, and its own on other antennas, stays.
            if swaps or held or run.get("template") == "rain-fade-swap":

                def theirs(s: dict[str, Any]) -> bool:
                    return s["provenance"]["workflow"] != run.get("workflow") or (
                        keep is not None and s["entity"]["antenna_id"] not in keep
                    )

                self.swaps = {k: s for k, s in self.swaps.items() if theirs(s)} | swaps
                self.held = {k: s for k, s in self.held.items() if theirs(s)} | held
            # A pass-yield run is its workflow's whole pass set, an empty one included: what it
            # dropped, or now holds, stops answering from the old run. Other workflows' passes
            # stay. A run of anything else (a held swap) drops only the passes it holds.
            if by_antenna or run.get("template") == "pass-yield":
                for antenna in set(self.passes) | set(by_antenna):
                    if keep is not None and antenna not in keep:
                        continue
                    others = [
                        p
                        for p in self.passes.get(antenna, [])
                        if p["provenance"]["workflow"] != run.get("workflow")
                    ]
                    mine = by_antenna.get(antenna, [])
                    self.passes[antenna] = sorted(others + mine, key=lambda i: i["target_at"])
                    if mine:
                        self.mappings[antenna] = mapping
            elif held:
                for antenna, found in self.passes.items():
                    self.passes[antenna] = [
                        p
                        for p in found
                        if p["entity"]["id"] not in held
                        or p["provenance"]["workflow"] != run.get("workflow")
                    ]
            self.capacity.update(capacity)
            self.modcod.update(modcod)
        used = (
            len(swaps)
            + len(held)
            + sum(map(len, by_antenna.values()))
            + sum(map(len, capacity.values()))
            + sum(map(len, modcod.values()))
        )
        return used, malformed

    def saved(self, now: datetime) -> dict[str, Any]:
        """What a restart needs to show and decide as before: everything not yet over."""

        def live(found: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in found.items() if when(v["until"]) > now}

        with self.lock:
            return {
                "passes": {
                    a: [p for p in ps if when(p["until"]) > now] for a, ps in self.passes.items()
                },
                "mappings": {a: None if m is FALLBACK else m for a, m in self.mappings.items()},
                "swaps": live(self.swaps),
                "held": live(self.held),
                "capacity": {a: live(days) for a, days in self.capacity.items()},
                "modcod": {a: live(plans) for a, plans in self.modcod.items()},
                "acted": dict(self.acted),
            }

    def restore(self, saved: dict[str, Any]) -> None:
        with self.lock:
            self.passes, self.swaps = saved["passes"], saved["swaps"]
            # A state file from before held swaps were kept has none.
            self.held = saved.get("held", {})
            self.acted = saved.get("acted", {})
            self.capacity, self.modcod = saved["capacity"], saved["modcod"]
            self.mappings = {a: m or FALLBACK for a, m in saved["mappings"].items()}

    def mapping(self, antenna: str) -> tuple[dict[str, Any], str]:
        """The mapping of the workflow whose passes this antenna shows, and where it came from."""
        with self.lock:
            found = self.mappings.get(antenna)
        return (found, "workflow") if found and found is not FALLBACK else (FALLBACK, "env")

    def find(
        self, antenna: str, satellite: str, aos: datetime | None, now: datetime
    ) -> dict[str, Any] | None:
        """The forecast pass a task means: that satellite on that antenna, at the AOS given or
        else the next one still to come."""
        with self.lock:
            candidates = [
                p
                for p in self.passes.get(antenna, [])
                if p["entity"]["satellite_id"] == satellite and when(p["until"]) > now
            ]
        if aos is not None:
            candidates = [p for p in candidates if abs(when(p["target_at"]) - aos) <= AOS_SLACK]
        return candidates[0] if candidates else None

    def upcoming(self, antenna: str, now: datetime) -> list[dict[str, Any]]:
        n = self.mapping(antenna)[0]["passes_listed"]
        with self.lock:
            return [p for p in self.passes.get(antenna, []) if when(p["until"]) > now][:n]

    def antennas(self) -> set[str]:
        """Every antenna a workflow sent this bridge something for."""
        with self.lock:
            swaps = {
                s["entity"]["antenna_id"] for s in [*self.swaps.values(), *self.held.values()]
            }
            return set(self.passes) | set(self.capacity) | set(self.modcod) | swaps

    def capacity_days(self, antenna: str, now: datetime) -> list[dict[str, Any]]:
        with self.lock:
            days = self.capacity.get(antenna, {})
            return [days[d] for d in sorted(days) if when(days[d]["until"]) > now][:CAPACITY_DAYS]

    def modcod_plans(self, antenna: str, now: datetime) -> list[dict[str, Any]]:
        n = self.mapping(antenna)[0]["passes_listed"]
        with self.lock:
            plans = self.modcod.get(antenna, {}).values()
            return sorted(
                (p for p in plans if when(p["until"]) > now), key=lambda p: p["target_at"]
            )[:n]

    def open_swaps(self, antenna: str, now: datetime) -> list[dict[str, Any]]:
        """Swaps advised for this antenna's passes whose cancel deadline is still ahead."""
        if not self.mapping(antenna)[0]["advisories"]:
            return []
        with self.lock:
            return sorted(
                (
                    s
                    for s in self.swaps.values()
                    if s["entity"]["antenna_id"] == antenna and when(s["action"]["deadline"]) > now
                ),
                key=lambda s: s["target_at"],
            )

    def held_swaps(self, antenna: str, now: datetime) -> list[dict[str, Any]]:
        """Swaps held for approval in Constellation on this antenna's passes still to start: the
        held action carries no deadline, so each lasts until its pass's AOS."""
        if not self.mapping(antenna)[0]["advisories"]:
            return []
        with self.lock:
            return [
                s
                for s in self.held.values()
                if s["entity"]["antenna_id"] == antenna and when(s["target_at"]) > now
            ]


# --- Lattice entities -----------------------------------------------------------------------


def entity_id(antenna: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"antenna|{antenna}"))


def site_id(station: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"site|{station}"))


def pass_entity_id(pass_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"pass|{pass_id}"))


def rain_entity_id(pass_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"rain|{pass_id}"))


def satellite_entity_id(satellite: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"satellite|{satellite}"))


def _gb(gb: dict[str, float]) -> str:
    return f"p10 {gb['p10']:.1f} / p50 {gb['p50']:.1f} / p90 {gb['p90']:.1f} GB"


def _advice(swap: dict[str, Any]) -> str:
    action = swap["action"]
    backup = (
        f"book {swap['entity']['satellite_id']} on {action['to_antenna_id']} at "
        f"{action['to_at'][:16]}Z"
        if action.get("to_antenna_id")
        else "no better pass within 6 h"
    )
    return f"{backup}; cancel by {action['deadline'][:16]}Z"


def _swap_alert(s: dict[str, Any]) -> dict[str, Any]:
    """An open swap's alert: approved in Constellation is an instruction (a warning), else advice
    (a caution)."""
    if results.action_state(s) == "approved":
        return {
            "alertCode": "CONSTELLATION_SWAP_APPROVED",
            "description": f"Swap approved in Constellation for {s['entity']['satellite_id']} "
            f"at {s['target_at'][:16]}Z ({_gb(gb_of(s))}): {_advice(s)}",
            "level": "ALERT_LEVEL_WARNING",
            "activeConditions": [{"conditionCode": "SWAP_APPROVED", "description": _advice(s)}],
        }
    return {
        "alertCode": "CONSTELLATION_RAIN_FADE_RISK",
        "description": f"Forecast rain-fade risk for {s['entity']['satellite_id']} "
        f"at {s['target_at'][:16]}Z ({_gb(gb_of(s))}): {_advice(s)}",
        "level": "ALERT_LEVEL_CAUTION",
        "activeConditions": [{"conditionCode": "SWAP_ADVISED", "description": _advice(s)}],
    }


def _asset(now: datetime, name: str, kind: str) -> dict[str, Any]:
    return {
        "isLive": True,
        "createdTime": iso(now),
        "expiryTime": iso(now + timedelta(seconds=PUBLISH_S * 3)),
        "aliases": {"name": name},
        "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_LAND"},
        "ontology": {"platformType": kind, "template": "TEMPLATE_ASSET"},
        "provenance": {
            "integrationName": INTEGRATION,
            "dataType": "constellation-forecast",
            "sourceUpdateTime": iso(now),
        },
    }


def position(station: dict[str, Any]) -> dict[str, Any]:
    return {
        "latitudeDegrees": station["latitude_deg"],
        "longitudeDegrees": station["longitude_deg"],
        "altitudeHaeMeters": station.get("altitude_m") or 0.0,
    }


def altitude_km(tle: dict[str, str]) -> float:
    """A satellite's mean altitude from its element set's mean motion."""
    n = float(tle["line2"][52:63]) * 2 * math.pi / 86400
    return (MU_KM3_S2 / n**2) ** (1 / 3) - EARTH_RADIUS_KM


def coverage_m(mask_deg: float, altitude: float) -> float:
    """How far over the ground an antenna sees a satellite at `altitude` km above its mask."""
    mask = math.radians(mask_deg)
    angle = math.acos(EARTH_RADIUS_KM * math.cos(mask) / (EARTH_RADIUS_KM + altitude)) - mask
    return round(EARTH_RADIUS_KM * angle * 1000)


def stations_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    """The topology's ground stations, each antenna's coverage drawn for the fleet's median
    altitude (550 km when no satellite has an element set)."""
    heights = sorted(
        altitude_km(s["tle"]) for s in document.get("satellites", {}).values() if s.get("tle")
    )
    altitude = heights[len(heights) // 2] if heights else 550.0
    return [
        {
            **gs,
            "ground_station_id": gs_id,
            "antenna_ids": sorted(gs.get("antennas", {})),
            "coverage_m": {
                a: coverage_m(float(spec.get("min_elevation_deg", 10)), altitude)
                for a, spec in gs.get("antennas", {}).items()
            },
        }
        for gs_id, gs in sorted(document.get("ground_stations", {}).items())
    ]


def _tle_exponent(field: str) -> float:
    """A TLE's implied-decimal field, e.g. " 44081-2" for 0.44081e-2."""
    return float(field[:-2].strip() or 0) / 1e5 * 10 ** int(field[-2:])


def orbit(tle: dict[str, str], now: datetime) -> dict[str, Any]:
    """An element set as Lattice's orbit: CCSDS mean elements for SGP4, read from the TLE."""
    l1, l2 = tle["line1"], tle["line2"]
    year = int(l1[18:20])
    epoch = datetime(year + (2000 if year < 57 else 1900), 1, 1, tzinfo=UTC) + timedelta(
        days=float(l1[20:32]) - 1
    )
    return {
        "orbitMeanElements": {
            "metadata": {
                "creationDate": iso(now),
                "originator": "Constellation",
                "refFrame": "ECI_REFERENCE_FRAME_TEME",
                "meanElementTheory": "MEAN_ELEMENT_THEORY_SGP4",
            },
            "meanKeplerianElements": {
                "epoch": iso(epoch),
                "meanMotion": float(l2[52:63]),
                "eccentricity": float("0." + l2[26:33].strip()),
                "inclinationDeg": float(l2[8:16]),
                "raOfAscNodeDeg": float(l2[17:25]),
                "argOfPericenterDeg": float(l2[34:42]),
                "meanAnomalyDeg": float(l2[43:51]),
            },
            "tleParameters": {
                "ephemerisType": 0,
                "classificationType": l1[7],
                "noradCatId": int(l1[2:7]),
                "elementSetNo": int(l1[64:68]),
                "revAtEpoch": int(l2[63:68]),
                "bstar": _tle_exponent(l1[53:61]),
                "meanMotionDot": float(l1[33:43]),
            },
        }
    }


def propagate(tle: dict[str, str], at: datetime) -> dict[str, Any]:
    """Where a satellite is at `at`, by SGP4 (the sgp4 package, the [lattice] extra): WGS84
    latitude, longitude and altitude, and its velocity over the ground in metres per second,
    east-north-up. Raises ValueError when SGP4 cannot place it (e.g. decayed)."""
    from sgp4.api import Satrec, jday  # type: ignore[import-not-found]

    jd, fr = jday(at.year, at.month, at.day, at.hour, at.minute, at.second + at.microsecond / 1e6)
    error, r, v = Satrec.twoline2rv(tle["line1"], tle["line2"]).sgp4(jd, fr)
    if error:
        raise ValueError(f"SGP4 cannot place it (error {error})")
    return _earth_fixed(list(r), list(v), at)


def _earth_fixed(eci: list[float], veci: list[float], at: datetime) -> dict[str, Any]:
    """A position and velocity in the inertial frame (km, km/s) as WGS84 latitude, longitude and
    altitude, and velocity over the ground east-north-up in m/s."""  # Greenwich mean sidereal time (IAU 1982), then inertial to Earth-fixed.
    days = (at - datetime(2000, 1, 1, 12, tzinfo=UTC)).total_seconds() / 86400
    gmst = math.radians((280.46061837 + 360.98564736629 * days) % 360)
    cg, sg = math.cos(gmst), math.sin(gmst)
    x, y, z = cg * eci[0] + sg * eci[1], -sg * eci[0] + cg * eci[1], eci[2]
    vxe = cg * veci[0] + sg * veci[1] + EARTH_RATE_RAD_S * y
    vye = -sg * veci[0] + cg * veci[1] - EARTH_RATE_RAD_S * x
    vze = veci[2]
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(5):
        nn = EARTH_RADIUS_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        lat = math.atan2(z + WGS84_E2 * nn * math.sin(lat), p)
    nn = EARTH_RADIUS_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - nn
    sl, cl, sn, cn = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    return {
        "latitude_deg": math.degrees(lat),
        "longitude_deg": math.degrees(lon),
        "altitude_m": alt * 1000,
        "velocity_enu": {
            "e": (-sn * vxe + cn * vye) * 1000,
            "n": (-sl * cn * vxe - sl * sn * vye + cl * vze) * 1000,
            "u": (cl * cn * vxe + cl * sn * vye + sl * vze) * 1000,
        },
    }


def satellite_location(tle: dict[str, str], at: datetime) -> dict[str, Any]:
    now = propagate(tle, at)
    return {
        "position": {
            "latitudeDegrees": now["latitude_deg"],
            "longitudeDegrees": now["longitude_deg"],
            "altitudeHaeMeters": now["altitude_m"],
        },
        "velocityEnu": now["velocity_enu"],
    }


def satellite_entities(document: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """The fleet's satellites that have an element set, each with its orbit."""
    return [
        {
            **_asset(now, name, "SATELLITE"),
            "entityId": satellite_entity_id(name),
            "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_SPACE"},
            "description": "Satellite in Constellation's fleet, placed from its element set.",
            "orbit": orbit(sat["tle"], now),
            "tle": sat["tle"],
        }
        for name, sat in sorted(document.get("satellites", {}).items())
        if sat.get("tle")
    ]


def site_entity(station: dict[str, Any], now: datetime) -> dict[str, Any]:
    gs = station["ground_station_id"]
    return {
        **_asset(now, gs, GROUND_STATION_ICON),
        "ontology": {
            "platformType": GROUND_STATION_ICON,
            "specificType": "Satellite ground station",
            "template": "TEMPLATE_ASSET",
        },
        "entityId": site_id(gs),
        # The station is the asset marker on the map; its antennas share its spot and carry no
        # location, so they are not drawn as a pile on top of it.
        "location": {"position": position(station)},
        "symbology": {"milStd2525C": {"sidc": GROUND_STATION_SIDC}},
        "description": f"Ground station {gs}, antennas: {', '.join(station['antenna_ids'])}",
        "relationships": {
            "relationships": [
                {
                    "relatedEntityId": entity_id(a),
                    "relationshipId": f"{site_id(gs)}|{entity_id(a)}",
                    "relationshipType": {"groupChild": {}},
                }
                for a in station["antenna_ids"]
            ]
        },
    }


def _row(row_id: str, name: str, message: str, now: datetime) -> dict[str, Any]:
    """A status row of the forecast service: always healthy, since it reports a forecast, not the
    service failing; what the forecast says is in its name and message."""
    status = "HEALTH_STATUS_HEALTHY"
    return {
        "id": row_id,
        "name": name,
        "health": status,
        "messages": [{"status": status, "message": message}],
        "updateTime": iso(now),
    }


def pass_row(p: dict[str, Any], floor: float, now: datetime) -> dict[str, Any]:
    gb, e = gb_of(p), p["entity"]
    ok = gb["p10"] >= floor
    elevation = results.max_elevation(p)
    return _row(
        f"pass|{e['id']}",
        f"Pass {e['satellite_id']} · {p['target_at'][11:16]}Z · {gb['p50']:.1f} GB "
        f"(p10 {gb['p10']:.1f})" + ("" if ok else " · below floor"),
        f"p10 {gb['p10']:.1f} GB {'clears' if ok else 'is below'} the {floor:.1f} GB floor"
        + (f"; max elevation {elevation:.0f}°" if elevation is not None else "")
        + f"; LOS {p['until'][11:16]}Z; run {p.get('provenance', {}).get('run_id')}",
        now,
    )


def modcod_row(p: dict[str, Any], now: datetime) -> dict[str, Any]:
    steps = p["series"]
    changes = len(steps) - 1
    return _row(
        f"modcod|{p['entity']['id']}",
        f"MODCOD {p['entity']['satellite_id']} · {p['target_at'][11:16]}Z · {changes} "
        f"change{'' if changes == 1 else 's'}",
        " → ".join(f"{s['value']['modcod']} at {s['t'][11:16]}Z" for s in steps),
        now,
    )


def capacity_row(c: dict[str, Any], now: datetime) -> dict[str, Any]:
    v, day = c["value"], c["target_at"][:10]
    return _row(
        f"capacity|{day}",
        f"Capacity {day} · {v['p50']:.1f} GB · {v['passes']} passes",
        f"p10 {v['p10']:.1f} / p50 {v['p50']:.1f} / p90 {v['p90']:.1f} GB over "
        f"{v.get('sellable_min', 0):.0f} sellable minutes",
        now,
    )


def antenna_entity(
    station: dict[str, Any], antenna: str, forecast: Forecast, now: datetime
) -> dict[str, Any]:
    """The forecast service for one antenna: the agent ReservePass tasks go to, carrying no
    location and no health of the dish. C2's asset panel shows its description (one line per
    listed pass) and its alerts (an advisory for a pass below the workflow's floor, a caution for
    an open rain-fade swap, a warning for one approved in Constellation); its status rows hold
    every listed pass, MODCOD plan and day of capacity, and how many swaps await approval."""
    floor = forecast.mapping(antenna)[0]["min_p10_gb"]
    swaps = forecast.open_swaps(antenna, now)
    upcoming = forecast.upcoming(antenna, now)
    summary = " · ".join(
        f"{p['entity']['satellite_id']} {p['target_at'][11:16]}Z {gb_of(p)['p50']:.1f} GB "
        f"(p10 {gb_of(p)['p10']:.1f}) "
        + ("ok" if gb_of(p)["p10"] >= floor else f"below the {floor:.1f} GB floor")
        for p in upcoming
    )
    rows = (
        [pass_row(p, floor, now) for p in upcoming]
        + [modcod_row(p, now) for p in forecast.modcod_plans(antenna, now)]
        + [capacity_row(c, now) for c in forecast.capacity_days(antenna, now)]
    )
    if len(swaps) > ALERTS_SHOWN:
        more = f"{len(swaps) - ALERTS_SHOWN} more rain-fade swaps open"
        rows.append(_row("swaps|more", more, more, now))
    held = forecast.held_swaps(antenna, now)
    if held:
        waiting = (
            f"{len(held)} rain-fade swap{'s' if len(held) != 1 else ''} awaiting approval "
            "in Constellation"
        )
        rows.append(_row("swaps|held", waiting, waiting, now))
    gs = station["ground_station_id"]
    low = [p for p in upcoming if gb_of(p)["p10"] < floor]
    return {
        **_asset(now, f"Constellation forecast · {antenna}", "Constellation Forecast"),
        "ontology": {
            "platformType": "Constellation Forecast",
            "specificType": f"Forecast service for antenna {antenna} at {gs}",
            "template": "TEMPLATE_ASSET",
        },
        "entityId": entity_id(antenna),
        "description": f"Constellation's forecast for antenna {antenna} at {gs}, not the antenna "
        "itself: "
        + (summary if summary else "no forecast passes yet")
        + ". Forecasts, not scheduled contacts. Task ReservePass with satelliteId and, optionally,"
        " aos (RFC 3339).",
        "health": {
            "connectionStatus": "CONNECTION_STATUS_ONLINE",
            # The forecast service's health: up while the bridge publishes it.
            "healthStatus": "HEALTH_STATUS_HEALTHY",
            "updateTime": iso(now),
            "components": rows,
            # Only open alerts: an antenna with no swap advised carries none, which clears it.
            "activeAlerts": [_swap_alert(s) for s in swaps[:ALERTS_SHOWN]]
            + [
                {
                    "alertCode": "CONSTELLATION_LOW_YIELD_FORECAST",
                    "description": f"Forecast: {p['entity']['satellite_id']} at "
                    f"{p['target_at'][11:16]}Z is expected to deliver {_gb(gb_of(p))}, below the "
                    f"{floor:.1f} GB floor",
                    "level": "ALERT_LEVEL_ADVISORY",
                    "activeConditions": [
                        {"conditionCode": "BELOW_FLOOR", "description": f"p10 below {floor:.1f} GB"}
                    ],
                }
                for p in low[:ALERTS_SHOWN]
            ],
        },
        **(
            {
                "relationships": {
                    "relationships": [
                        {
                            "relatedEntityId": site_id(gs),
                            "relationshipId": f"{entity_id(antenna)}|{site_id(gs)}",
                            "relationshipType": {"groupParent": {}},
                        }
                    ]
                }
            }
            if PUBLISH_STATIONS
            else {}
        ),
        "taskCatalog": {"taskDefinitions": [{"taskSpecificationUrl": TASK_TYPE}]},
    }


def pass_entity(
    p: dict[str, Any], site: dict[str, Any], now: datetime, live: bool = True
) -> dict[str, Any]:
    """One forecast pass, a member of its antenna, from now until shortly after LOS: a line
    along the ground under the satellite from AOS to LOS (the API's track), or a point at its
    station (`site`, a position) when the pass has none. Its window is a schedule from AOS for
    the pass's length; Lattice has no field for expected GB, so it is in the name."""
    e = p["entity"]
    aos, los = when(p["target_at"]), when(p["until"])
    gb = gb_of(p)
    track = e.get("track") or []
    shape: dict[str, Any] = (
        {
            "geoShape": {
                "line": {
                    "positions": [
                        {
                            "latitudeDegrees": t["latitude_deg"],
                            "longitudeDegrees": t["longitude_deg"],
                        }
                        for t in track
                    ]
                }
            }
        }
        if len(track) >= 2
        else {"geoShape": {"point": {"position": site}}, "location": {"position": site}}
    )
    return {
        "entityId": pass_entity_id(e["id"]),
        "description": f"Constellation forecast, not a scheduled contact: {e['satellite_id']} over "
        f"{e['antenna_id']} AOS {p['target_at'][11:16]}Z to LOS {p['until'][11:16]}Z.",
        "isLive": live,
        "createdTime": iso(now),
        "expiryTime": iso(max(los, now) + timedelta(minutes=2)),
        "aliases": {
            "name": f"Forecast · {e['satellite_id']} @ {e['antenna_id']} "
            f"{p['target_at'][11:16]}Z · {gb['p50']:.1f} GB (p10 {gb['p10']:.1f})"
        },
        "ontology": {"platformType": "Forecast Pass", "template": "TEMPLATE_GEO"},
        **shape,
        "geoDetails": {"type": "GEO_TYPE_GENERAL", "visualDetails": {"lineColor": FORECAST_COLOR}},
        "schedules": {
            "schedules": [
                {
                    "scheduleId": e["id"],
                    "scheduleType": "SCHEDULE_TYPE_ZONE_ENABLED",
                    "windows": [
                        {
                            "cronExpression": f"{aos.second} {aos.minute} {aos.hour} {aos.day} "
                            f"{aos.month} ? {aos.year}",
                            "durationMillis": str(int((los - aos).total_seconds() * 1000)),
                        }
                    ],
                }
            ]
        },
        "relationships": {
            "relationships": [
                {
                    "relatedEntityId": entity_id(e["antenna_id"]),
                    "relationshipId": f"{pass_entity_id(e['id'])}|{entity_id(e['antenna_id'])}",
                    "relationshipType": {"groupParent": {}},
                }
            ]
        },
        "provenance": {
            "integrationName": INTEGRATION,
            "dataType": "constellation-forecast",
            "sourceUpdateTime": iso(now),
        },
    }


def coverage_entity(station: dict[str, Any], now: datetime) -> dict[str, Any]:
    """How far the station's widest-seeing antenna sees the fleet above its mask: a forecast
    circle, not the station's own coverage claim."""
    gs = station["ground_station_id"]
    reach = max((station.get("coverage_m") or {}).values(), default=0)
    return {
        "entityId": str(uuid.uuid5(NAMESPACE, f"coverage|{gs}")),
        "isLive": True,
        "createdTime": iso(now),
        "expiryTime": iso(now + timedelta(seconds=PUBLISH_S * 3)),
        "aliases": {"name": f"Forecast · coverage · {gs}"},
        "description": f"Where {gs} sees the fleet above its elevation mask, at the fleet's "
        "median altitude.",
        "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_LAND"},
        "ontology": {"platformType": "Forecast Coverage", "template": "TEMPLATE_GEO"},
        "location": {"position": position(station)},
        "geoShape": {
            "ellipse": {"semiMajorAxisM": reach, "semiMinorAxisM": reach, "orientationD": 0}
        },
        "geoDetails": {
            "type": "GEO_TYPE_GENERAL",
            "visualDetails": {
                "lineColor": FORECAST_COLOR,
                "fillColor": {**FORECAST_COLOR, "alpha": 0.04},
            },
        },
        "provenance": {
            "integrationName": INTEGRATION,
            "dataType": "constellation-forecast",
            "sourceUpdateTime": iso(now),
        },
    }


def rain_entity(
    swap: dict[str, Any], site: dict[str, Any], now: datetime, live: bool = True
) -> dict[str, Any]:
    """A rain-fade swap as a hazard over the station whose pass it fades, until shortly after
    that pass's LOS. The forecast attenuation goes in the name when the ledger has it."""
    e = swap["entity"]
    approved = results.action_state(swap) == "approved"
    fade = ((swap.get("explain") or {}).get("weather") or {}).get("value")
    db = f" · {fade['p50']:.1f} dB" if isinstance(fade, dict) and "p50" in fade else ""
    return {
        "entityId": rain_entity_id(e["id"]),
        "isLive": live,
        "createdTime": iso(now),
        "expiryTime": iso(max(when(swap["until"]), now) + timedelta(minutes=2)),
        "aliases": {
            "name": f"Forecast · rain fade{', swap approved' if approved else ''} · "
            f"{e['satellite_id']} @ {e['antenna_id']} {swap['target_at'][11:16]}Z{db}"
        },
        "description": f"Forecast rain fade at {e['ground_station_id']}: "
        f"{'swap approved in Constellation: ' if approved else ''}{_advice(swap)}.",
        "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_LAND"},
        "ontology": {"platformType": "Forecast Rain Fade", "template": "TEMPLATE_GEO"},
        "location": {"position": site},
        "geoShape": {
            "ellipse": {
                "semiMajorAxisM": RAIN_RADIUS_M,
                "semiMinorAxisM": RAIN_RADIUS_M,
                "orientationD": 0,
            }
        },
        "geoDetails": {
            "type": "GEO_TYPE_HAZARD",
            "visualDetails": {
                "lineColor": FORECAST_COLOR,
                "fillColor": {**FORECAST_COLOR, "alpha": 0.2},
            },
        },
        "provenance": {
            "integrationName": INTEGRATION,
            "dataType": "constellation-forecast",
            "sourceUpdateTime": iso(now),
        },
    }


def publish_satellites(satellites: list[dict[str, Any]], now: datetime) -> int:
    """Each satellite where it is now, when this Lattice holds none of its own; Lattice draws an
    entity where its location says, never from its orbit."""
    if not PUBLISH_SATELLITES:
        return 0
    count = 0
    for sat in satellites:
        try:
            location = satellite_location(sat["tle"], now)
        except ValueError as exc:
            warn(f"satellite {sat['aliases']['name']} not published: {exc}")
            continue
        body = {k: v for k, v in sat.items() if k != "tle"}
        lattice(
            "PUT",
            "/entities",
            {
                **body,
                "location": location,
                "provenance": {**sat["provenance"], "sourceUpdateTime": iso(now)},
                # A moving thing goes stale fast: gone a minute after the bridge stops moving it.
                "expiryTime": iso(now + timedelta(seconds=max(60, SATELLITE_S * 6))),
            },
        )
        count += 1
    return count


def move_satellites(satellites: list[dict[str, Any]]) -> None:
    while True:
        try:
            publish_satellites(satellites, datetime.now(UTC))
        except Exception as exc:
            warn(f"satellites: {type(exc).__name__}: {exc}")
        time.sleep(SATELLITE_S)


def publish(
    stations: list[dict[str, Any]],
    forecast: Forecast,
    shown: dict[str, Any],
    satellites: list[dict[str, Any]] | None = None,
) -> int:
    """Every site, antenna, listed pass, open rain-fade hazard and satellite; passes and
    hazards shown last time and not now are withdrawn. `shown` is those live from the last
    publish, keyed pass|id and rain|id, updated in place."""
    now = datetime.now(UTC)
    count = 0
    listed: dict[str, Any] = {}
    sites = {a: position(s) for s in stations for a in s["antenna_ids"]}
    # The network config lists everything the account has, canaries and test antennas too:
    # Lattice shows only what a workflow sends this bridge forecasts for.
    forecast_for = forecast.antennas()
    forecasting = [
        {**s, "antenna_ids": [a for a in s["antenna_ids"] if a in forecast_for]} for s in stations
    ]
    for station in (s for s in forecasting if s["antenna_ids"]):
        if PUBLISH_STATIONS:
            lattice("PUT", "/entities", site_entity(station, now))
            count += 1
        if PUBLISH_COVERAGE and station.get("coverage_m"):
            lattice("PUT", "/entities", coverage_entity(station, now))
            count += 1
        for antenna in station["antenna_ids"]:
            lattice("PUT", "/entities", antenna_entity(station, antenna, forecast, now))
            count += 1
            if not forecast.mapping(antenna)[0]["pass_entities"]:
                continue
            for p in forecast.upcoming(antenna, now):
                lattice("PUT", "/entities", pass_entity(p, sites[antenna], now))
                listed[f"pass|{p['entity']['id']}"] = p
                count += 1
            for s in forecast.open_swaps(antenna, now):
                lattice("PUT", "/entities", rain_entity(s, sites[antenna], now))
                listed[f"rain|{s['entity']['id']}"] = s
                count += 1
    count += publish_satellites(satellites or [], now)
    for key, p in shown.items():
        if key not in listed and when(p["until"]) > now:
            # isLive false on a live entity is Lattice's delete.
            # An antenna gone from the network config leaves its passes to expire at LOS.
            if site := sites.get(p["entity"]["antenna_id"]):
                withdrawn = rain_entity if key.startswith("rain|") else pass_entity
                lattice("PUT", "/entities", withdrawn(p, site, now, live=False))
    shown.clear()
    shown.update(listed)
    return count


# --- Lattice tasks --------------------------------------------------------------------------


def decide(task: dict[str, Any], antenna: str, forecast: Forecast, now: datetime) -> dict[str, Any]:
    """ALLOW or DENY a ReservePass task by Constellation's forecast for that pass, at the floor
    of the workflow that forecast came from."""
    spec = task.get("specification") or {}
    # Over Lattice's REST API a task arrives as a google.protobuf.Struct, its fields under "value".
    if isinstance(spec.get("value"), dict):
        spec = spec["value"]
    satellite = spec.get("satelliteId") or spec.get("satellite_id")
    aos_text = spec.get("aos")
    aos = when(aos_text) if aos_text else None
    mapping, source = forecast.mapping(antenna)
    floor = mapping["min_p10_gb"]
    decision: dict[str, Any] = {
        "schema": "constellation.workflow.decision.v1",
        "task_id": task["version"]["taskId"],
        "antenna_id": antenna,
        "satellite_id": satellite,
        "min_p10_gb": floor,
        "floor_from": source,
        "max_forecast_age_s": MAX_FORECAST_AGE_S,
        "decided_at": iso(now),
    }
    found = forecast.find(antenna, satellite, aos, now) if satellite else None
    if found is None:
        return {
            **decision,
            "decision": "DENY",
            "diagnostic": f"NO_FORECAST: Constellation has no forecast pass for {satellite} on {antenna}",
        }
    gb = gb_of(found)
    decision |= {
        "pass_id": found["entity"]["id"],
        "aos": found["target_at"],
        "los": found["until"],
        "expected_gb": gb,
        "provenance": found.get("provenance", {}),
        "explain": found.get("explain", {}),
    }
    swap = forecast.swaps.get(found["entity"]["id"]) if mapping["advisories"] else None
    approved = swap is not None and results.action_state(swap) == "approved"
    if approved and when(swap["action"]["deadline"]) > now:
        # A person approved swapping this pass away in Constellation: it is not to be flown.
        # Past the cancel deadline the swap is closed, as its alert and the next swap run are.
        return {
            **decision,
            "decision": "DENY",
            "diagnostic": f"SWAP_APPROVED: a swap of this pass was approved in Constellation: "
            f"{_advice(swap)}",
        }
    if swap and not approved:
        decision["advisory"] = f"rain-fade risk: {_advice(swap)}"
    made = decision["provenance"].get("made_at")
    if made is None or now - when(made) > timedelta(seconds=MAX_FORECAST_AGE_S):
        return {
            **decision,
            "decision": "DENY",
            "diagnostic": f"STALE_FORECAST: the forecast for this pass was made "
            f"{'at ' + made if made else 'at a time this bridge did not record'}, longer ago "
            f"than MAX_FORECAST_AGE_S={MAX_FORECAST_AGE_S:.0f} s allows",
        }
    if gb["p10"] < floor:
        return {
            **decision,
            "decision": "DENY",
            "diagnostic": f"EXPECTED_GB_BELOW_FLOOR: p10={gb['p10']:.1f} GB < min={floor:.1f} GB "
            f"(p50={gb['p50']:.1f}, p90={gb['p90']:.1f})",
        }
    return {
        **decision,
        "decision": "ALLOW",
        "diagnostic": f"p10={gb['p10']:.1f} GB >= min={floor:.1f} GB",
    }


def result(decision: dict[str, Any]) -> dict[str, Any]:
    """The decision as the task's result, a Struct Lattice can show without our schema: one line
    to read, then the numbers."""
    gb = decision.get("expected_gb")
    head = f"{decision['decision']} {decision['satellite_id']} on {decision['antenna_id']}"
    if gb:
        head += f" AOS {decision['aos'][11:16]}Z: {_gb(gb)}"
    summary = f"{head}; floor p10 {decision['min_p10_gb']:.1f} GB. {decision['diagnostic']}"
    if decision.get("advisory"):
        summary += f". {decision['advisory']}"
    allowed = decision["decision"] == "ALLOW"
    if allowed:
        summary += ". Nothing was reserved: book the pass in your scheduler"
    value = {
        "summary": summary,
        "decision": decision["decision"],
        # The bridge decides and books nothing, so DONE_OK never reads as a reservation made.
        "outcome": "ALLOWED_NOT_RESERVED" if allowed else "DENIED",
        "reserved": False,
        "minP10Gb": decision["min_p10_gb"],
        **({"passId": decision["pass_id"], "aos": decision["aos"], "expectedGb": gb} if gb else {}),
        **({"advisory": decision["advisory"]} if decision.get("advisory") else {}),
    }
    return {"@type": "type.googleapis.com/google.protobuf.Struct", "value": value}


def set_status(
    task_id: str,
    version: int,
    agent: str,
    status: str,
    error: str | None = None,
    code: str = "ERROR_CODE_REJECTED",
    outcome: dict[str, Any] | None = None,
) -> None:
    new: dict[str, Any] = {"status": status}
    if error:
        new["taskError"] = {"code": code, "message": error}
    if outcome:
        new["result"] = outcome
    lattice(
        "PUT",
        f"/tasks/{task_id}/status",
        {"statusVersion": version, "newStatus": new, "author": {"system": {"entityId": agent}}},
    )


def assignee(task: dict[str, Any]) -> str:
    return ((task.get("relations") or {}).get("assignee") or {}).get("system", {}).get(
        "entityId"
    ) or ""


def handle(
    task: dict[str, Any], agents: dict[str, str], forecast: Forecast
) -> dict[str, Any] | None:
    """One ExecuteRequest: EXECUTING, decide, upload the decision, then DONE_OK or DONE_NOT_OK."""
    agent = assignee(task)
    antenna = agents.get(agent)
    if antenna is None:
        return None
    task_id, status_version = (
        task["version"]["taskId"],
        int(task["version"].get("statusVersion", 0)),
    )
    set_status(task_id, status_version + 1, agent, "STATUS_EXECUTING")
    decision = decide(task, antenna, forecast, datetime.now(UTC))
    lattice(
        "POST",
        f"/objects/constellation-decisions-{decision['decision'].lower()}-{task_id}.json",
        raw=json.dumps(decision).encode(),
    )
    if decision["decision"] == "ALLOW":
        set_status(task_id, status_version + 2, agent, "STATUS_DONE_OK", outcome=result(decision))
    else:
        set_status(
            task_id,
            status_version + 2,
            agent,
            "STATUS_DONE_NOT_OK",
            decision["diagnostic"],
            outcome=result(decision),
        )
    log(f"{decision['decision']} task {task_id} on {antenna}: {decision['diagnostic']}")
    return decision


def fail(task: dict[str, Any], error: Exception) -> None:
    """A task the bridge could not decide is closed as failed with why, never left EXECUTING."""
    task_id = task["version"]["taskId"]
    warn(f"task {task_id}: {error}")
    try:
        now = lattice("GET", f"/tasks/{task_id}")
        if not now["status"]["status"].startswith("STATUS_DONE"):
            set_status(
                task_id,
                int(now["version"]["statusVersion"]) + 1,
                assignee(task),
                "STATUS_DONE_NOT_OK",
                f"BRIDGE_ERROR: {error}"[:500],
                code="ERROR_CODE_FAILED",
            )
    except Exception as exc:
        warn(f"task {task_id}: could not close it ({exc})")


def work(tasks: "queue.Queue[dict[str, Any]]", agents: dict[str, str], forecast: Forecast) -> None:
    seen: set[str] = set()
    while True:
        task = tasks.get()
        # A reconnected stream can deliver a task again; it is decided once.
        if task["version"]["taskId"] in seen:
            continue
        seen.add(task["version"]["taskId"])
        try:
            handle(task, agents, forecast)
        except Exception as exc:
            fail(task, exc)


def stream(agents: dict[str, str], tasks: "queue.Queue[dict[str, Any]]") -> None:
    """Lattice's agent stream. Lattice fails a task at once ("asset is offline") when no agent
    connection is open for its assignee, so the bridge holds one stream open and only queues
    tasks here; the long poll (/agent/listen) was closed while a task was handled."""
    while True:
        selected = sorted(agents)
        body = json.dumps({"agentSelector": {"entityIds": selected}}).encode()
        try:
            req = urllib.request.Request(
                f"{LATTICE}/api/v1/agent/stream",
                data=body,
                method="POST",
                headers={**lattice_headers(), "Content-Type": "application/json"},
            )
            # Lattice sends a heartbeat every few seconds, so a silent minute is a dead stream.
            with urllib.request.urlopen(req, timeout=60) as resp:
                log(f"listening for tasks on {len(agents)} antennas")
                for raw in resp:
                    # The network config changed the antennas: listen again for the new set.
                    if sorted(agents) != selected:
                        break
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    if event.get("executeRequest"):
                        tasks.put(event["executeRequest"]["task"])
        except urllib.error.HTTPError as exc:
            warn(f"agent stream: HTTP {exc.code} {exc.read()[:500].decode(errors='replace')}")
        except Exception as exc:
            warn(f"agent stream: {type(exc).__name__}: {exc}")
        time.sleep(1)


# --- telling Constellation ------------------------------------------------------------------


def check_in() -> bool:
    """Whether Constellation took the check-in."""
    try:
        constellation(
            "POST",
            "/predictions/feed/checkin",
            {
                "consumer": CONSUMER,
                "system": "lattice",
                "target": f"Lattice at {urllib.parse.urlsplit(LATTICE).hostname}",
                "bridge_version": VERSION,
                "mapping_versions": MAPPING_VERSIONS,
                "can": {"events": False, "timeline": False, "parameters": False, "buckets": True},
            },
        )
    except Exception as exc:
        warn(f"could not check in ({exc})")
        return False
    return True


def report(cursor: str, applied: dict[str, int]) -> None:
    try:
        target = f"Lattice at {urllib.parse.urlsplit(LATTICE).hostname}"
        results.report(constellation, CONSUMER, cursor, applied, target)
    except Exception as exc:
        warn(f"could not report delivery ({exc})")



def deliver(entry: dict[str, Any], forecast: Forecast) -> tuple[int, int, int, int]:
    """One feed entry (a run): each of its feed outputs addressed to this connection, kept by
    that output's Lattice mapping. Outputs for other connections are another bridge's; one to
    this connection for another system (a Yamcs output, as one saved without a mapping is) is
    refused, as the plan that allowed it is not Lattice's. Returns the results used, the outputs
    refused, the items skipped as malformed and the approved swaps it now enforces."""
    used = refused = malformed = acted = 0
    for resource, found in results.unpack(entry["otlp"], CONSUMER, log):
        if resource.get("constellation.system") != "lattice":
            warn(
                f"refused an output of run {resource.get('constellation.run_id')}: it is a "
                f"{resource.get('constellation.system')} output, not a Lattice one"
            )
            refused += 1
            continue
        output = json.loads(resource.get("constellation.output") or "{}")
        mapping = {**FALLBACK, **output.get("mapping", {})}
        run = {
            "template": resource.get("constellation.template"),
            "workflow": resource.get("constellation.workflow"),
            "run_id": resource.get("constellation.run_id"),
            "made_at": results.made_at(entry["otlp"]),
        }
        took, bad = forecast.take(found, mapping, run)
        used, malformed = used + took, malformed + bad
        acted += results.first_acted([i for i in found if is_swap(i)], forecast.acted)
    return used, refused, malformed, acted


def rest_of_run(cursor: str, page: int) -> dict[str, Any]:
    return results.rest_of_run(constellation, CONSUMER, cursor, page)


def load_state() -> dict[str, Any] | None:
    try:
        with open(CURSOR_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        warn(f"{CURSOR_FILE} is unreadable ({exc}): starting as a first start")
        return None


def save_state(cursor: str, forecast: Forecast, shown: dict[str, Any]) -> None:
    state = {"cursor": cursor, "forecast": forecast.saved(datetime.now(UTC)), "shown": shown}
    with open(f"{CURSOR_FILE}.tmp", "w") as f:
        json.dump(state, f)
    os.replace(f"{CURSOR_FILE}.tmp", CURSOR_FILE)


TOKEN_REJECTED = (
    "Lattice rejected the token (LATTICE_TOKEN; on Lattice Sandboxes also"
    " LATTICE_SANDBOX_TOKEN, which expire with the environment)"
)


def stop(message: str) -> None:
    warn(message)
    sys.exit(2)


def ready() -> dict[str, Any]:
    """The network config, once Constellation and Lattice both answer. What only the operator can
    fix here (a missing variable, a rejected Lattice token) exits 2 with one line, so a restart
    loop says why; an outage or a 5xx on either side is waited out, and a refused Constellation
    key long (constellation())."""
    for name in ("CONSTELLATION_API_KEY", "LATTICE_URL", "LATTICE_TOKEN"):
        if not os.environ.get(name, "").strip():
            stop(f"{name} is not set")
    if why := results.key_refusal(API):
        stop(why)
    if MIN_P10_GB is None:
        stop(
            "MIN_P10_GB is not a number (GB, at least 0): the expected GB p10 floor a"
            " ReservePass decision uses for an antenna no workflow has forecast yet; unset, it is 0"
        )
    if MAX_FORECAST_AGE_S is None:
        stop(
            "MAX_FORECAST_AGE_S is not a number (seconds, at least 0): the oldest forecast a"
            " ReservePass decision may use; unset, it is 21600 (6 hours)"
        )
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(f"{STATE_DIR}/.writable", "w") as probe:
            os.fsync(probe.fileno())
        os.remove(f"{STATE_DIR}/.writable")
    except OSError as exc:
        stop(
            f"STATE_DIR {STATE_DIR} is not a directory this bridge can write ({exc.strerror}):"
            " mount a volume there that user 65534 can write, as compose.yaml does"
        )
    if PUBLISH_SATELLITES:
        try:
            import sgp4  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            stop(
                "PUBLISH_SATELLITES needs the sgp4 package: install constellation-space[lattice]"
                " (the image has it)"
            )
    delay = 5
    while True:
        try:
            document = constellation("GET", "/topology/config")["document"]
            try:
                # A listing answers 200 only from a Lattice API that accepts the token.
                lattice("GET", "/objects?prefix=constellation-decisions-&maxPageSize=1")
            except HTTPFailure as exc:
                if exc.code in (401, 403):
                    stop(TOKEN_REJECTED)
                if exc.code == 404:
                    stop(
                        f"No Lattice API at LATTICE_URL {LATTICE} (GET /api/v1/objects answered"
                        " 404): set it to https://<environment>, without /api/v1"
                    )
                if exc.code < 500 and exc.code != 429:
                    stop(f"Lattice refused the startup check, so check LATTICE_URL: {exc}")
                raise
            return document
        except Exception as exc:
            warn(f"waiting for Constellation and Lattice, retrying in {delay}s ({exc})")
        time.sleep(delay)
        delay = min(delay * 2, 300)


def settings() -> str:
    """The decision settings in effect, each marked when it is the default."""
    return ", ".join(
        f"{name}={value:g}" + ("" if os.environ.get(name, "").strip() else " (default)")
        for name, value in (("MIN_P10_GB", MIN_P10_GB), ("MAX_FORECAST_AGE_S", MAX_FORECAST_AGE_S))
    )


def network(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """The network config's stations, satellites and taskable antennas (by agent id)."""
    stations = stations_of(document)
    agents = {entity_id(a): a for s in stations for a in s["antenna_ids"]}
    return stations, satellite_entities(document, datetime.now(UTC)), agents


def main() -> None:
    forecast = Forecast()
    document = ready()
    log(settings())
    stations, satellites, agents = network(document)
    configured = time.time()
    if PUBLISH_SATELLITES:
        threading.Thread(target=move_satellites, args=(satellites,), daemon=True).start()
    log(
        f"{API} -> {LATTICE} as {CONSUMER}: {len(agents)} antennas, state in {CURSOR_FILE}"
    )
    tasks: queue.Queue[dict[str, Any]] = queue.Queue()
    threading.Thread(target=work, args=(tasks, agents, forecast), daemon=True).start()
    saved = load_state()
    cursor: str | None = saved["cursor"] if saved else None
    if saved:
        log(f"resuming the feed after {cursor!r}")
        if saved.get("forecast"):
            forecast.restore(saved["forecast"])
    published, listening = 0.0, False
    # What the last publish put on the map, kept so a restart still withdraws what it drops.
    shown: dict[str, Any] = (saved or {}).get("shown") or {}
    unpublished: list[tuple[str, dict[str, int]]] = []
    # Checked in again every results.CHECKIN_S; a failed check-in is tried again after POLL_S,
    # doubling up to that.
    next_checkin, retry = 0.0, POLL_S
    while True:
        if time.time() >= next_checkin:
            if check_in():
                next_checkin, retry = time.time() + results.CHECKIN_S, POLL_S
            else:
                next_checkin, retry = time.time() + retry, min(retry * 2, results.CHECKIN_S)
        try:
            if time.time() - configured > CONFIG_S:
                # In place: the satellite, task and stream threads hold these.
                stations, satellites[:], fresh = network(
                    constellation("GET", "/topology/config")["document"]
                )
                for gone in agents.keys() - fresh.keys():
                    del agents[gone]
                agents.update(fresh)
                configured = time.time()
            if cursor is None:
                cursor = results.start_cursor(constellation, CONSUMER)
                log(f"first start: reading the feed after {cursor!r}")
            page = results.results_page(constellation, CONSUMER, cursor)
            if lost := results.expired(page, cursor):
                warn(lost)
            for entry in page["entries"]:
                entry = results.whole(entry, rest_of_run)
                used, refused, malformed, acted = deliver(entry, forecast)
                cursor = entry["cursor"]
                # Every run with a part for this connection is reported, also one Lattice draws
                # nothing for (a swap run advising nothing, an RFI run of links).
                if results.unpack(entry["otlp"], CONSUMER, lambda _: None):
                    counts = (("refused", refused), ("malformed", malformed), ("acted", acted))
                    applied = {"forecasts": used} | {k: v for k, v in counts if v}
                    unpublished.append((cursor, applied))
            # Catching up after a start reads on without publishing; Lattice gets the result once.
            if page["entries"]:
                continue
            try:
                if unpublished or time.time() - published > PUBLISH_S:
                    count = publish(stations, forecast, shown, satellites)
                    published = time.time()
                    for done, applied in unpublished:
                        report(done, {"entities": count, **applied})
                        log(f"published {count} entities with {applied}, {done}")
                    unpublished = []
                    save_state(cursor, forecast, shown)
            except HTTPFailure as exc:
                if exc.code in (401, 403):
                    stop(TOKEN_REJECTED)
                raise
            finally:
                # Tasks are taken once the antennas are in Lattice with this forecast; a publish
                # that fails still opens the stream, so tasks are decided rather than refused.
                if not listening:
                    threading.Thread(target=stream, args=(agents, tasks), daemon=True).start()
                    listening = True
            # Progress, not only an answer: a poll whose runs could not be applied, or that Lattice
            # did not take, leaves the heartbeat to go stale and the healthcheck marks it unhealthy.
            with open(HEARTBEAT, "a"):
                os.utime(HEARTBEAT)
        except Exception as exc:
            warn(f"{type(exc).__name__}: {exc}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
