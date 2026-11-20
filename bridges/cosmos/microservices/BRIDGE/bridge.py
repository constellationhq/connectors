"""Constellation workflow forecasts into OpenC3 COSMOS, run by COSMOS as a plugin microservice.

Pulls the Constellation feed outbound (GET /predictions/feed) as its own connection (CONSUMER),
reads only the part of each run a feed output node addressed to this connection (the
resourceLogs whose constellation.connection is CONSUMER; one for another system is refused and
reported, as the Yamcs and Lattice bridges do), and injects the forecast into the CONSTELLATION
target's packets (targets/CONSTELLATION/cmd_tlm/tlm.txt):

    NEXT_PASS  the next predicted pass and its expected GB (p10/p50/p90)
    NEXT_24H   passes and expected GB over the next 24 hours
    SWAP       the soonest rain-fade swap to decide, with minutes left (limits go yellow, then red)
    CAPACITY   one day's capacity across the antennas, and the tightest
    MODCOD     the next pass's MODCOD plan
    RUN        the latest workflow run received, of any kind

A packet is injected only once a run of its kind has arrived.

So COSMOS's own Telemetry Viewer, Telemetry Grapher, Limits Monitor and Data Extractor show the
forecast with no other install. It also writes each run, and an index of workflows, as JSON into
COSMOS's config bucket (<SCOPE>/constellation/), the documents the Yamcs bridge writes to its
bucket, which the plugin's Constellation Forecast tool reads through COSMOS's storage API. It checks in and reports what it wrote, like the Yamcs bridge.
The last cursor applied and the forecast it held are kept in the same bucket
(<SCOPE>/constellation/bridge-<consumer>.json), so a restart resumes there with the same packets;
a first start applies only the newest run for this connection.
The Constellation key comes from COSMOS's secrets (Admin > Secrets), read at each poll so a key
added or changed there is used without a reinstall, and never leaves this process.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from constellation_space import results

VERSION = "0.8.1"
TARGET = "CONSTELLATION"
POLL_S = int(os.environ.get("POLL_SECONDS", "15"))
# COSMOS shows a packet stale 30 s after its last value: re-inject every poll so a quiet feed
# still reads as current, and the next pass and minutes left keep moving.
REFRESH_S = 10
# How often, with no key or a refused one, it looks for a new key in Admin > Secrets.
KEY_WAIT_S = 60
SCOPE = os.environ.get("OPENC3_SCOPE", "DEFAULT")
# Where the Constellation Forecast tool (tools/constellationforecast) reads the documents.
DOCS = f"{SCOPE}/constellation"
# COSMOS's script API signs in with OPENC3_API_PASSWORD, which COSMOS gives a plugin microservice
# only as the service password; without it every call fails with "Token is required". Set here,
# on start: the first COSMOS import (writing the Forecast tool's documents) fixes the client's
# credentials for the life of the process.
if "OPENC3_SERVICE_PASSWORD" in os.environ:
    os.environ.setdefault("OPENC3_API_PASSWORD", os.environ["OPENC3_SERVICE_PASSWORD"])


# The bytes of each STRING item in tlm.txt, by item name (a test holds them to it). COSMOS drops
# a whole injected packet holding a longer value, and says so only in its decom log, and the API
# takes ids longer than these, so a value is cut to fit. Its OVERFLOW TRUNCATE does not do it for
# a string in the Python decom of 7.0 to 7.5.
STRING_BYTES = {
    "SATELLITE": 64,
    "ANTENNA": 64,
    "AOS": 32,
    "LOS": 32,
    "BACKUP_ANTENNA": 64,
    "BACKUP_AOS": 32,
    "DEADLINE": 32,
    "STATUS": 16,
    "DAY": 16,
    "TIGHTEST_ANTENNA": 64,
    "FIRST": 32,
    "LAST": 32,
    "WORKFLOW": 64,
    "TEMPLATE": 32,
    "RUN_ID": 64,
}


def fit(values: dict[str, Any]) -> dict[str, Any]:
    """The values with each string cut to its item's bytes, never mid-character."""
    return {
        k: v.encode()[: STRING_BYTES[k]].decode(errors="ignore") if k in STRING_BYTES else v
        for k, v in values.items()
    }


def inject(packet: str, values: dict[str, Any]) -> None:
    """COSMOS's inject_tlm, from whichever module this COSMOS exposes it in."""
    try:
        from openc3.script import inject_tlm  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - older COSMOS
        from openc3.api import inject_tlm  # type: ignore[import-not-found]
    inject_tlm(TARGET, packet, values)


def api_key() -> str:
    """CONSTELLATION_API_KEY as Admin > Secrets holds it now, else as the microservice started
    with it (SECRET ENV in plugin.txt; the only source outside COSMOS). Empty when neither has it."""
    try:
        from openc3.utilities.secrets import Secrets  # type: ignore[import-not-found]

        key = Secrets.get_client().get("CONSTELLATION_API_KEY", scope=SCOPE)
    except Exception:
        key = None
    return (key or os.environ.get("CONSTELLATION_API_KEY") or "").strip()


def call(method: str, path: str, body: Any = None) -> Any:
    api = os.environ["CONSTELLATION_API_URL"].rstrip("/")
    return results.http(method, f"{api}{path}", body, {"X-API-Key": api_key()})


def log(message: str, warn: bool = False) -> None:
    """A line in COSMOS's log messages (CmdTlmServer > Log Messages), where an operator looks.
    Outside COSMOS, stdout, or stderr for a warning. A plain print is not enough in COSMOS: its
    operator holds a microservice's stdout back until a few KB of it pile up."""
    line = f"constellation-bridge: {message}"
    try:
        from openc3.utilities.logger import Logger  # type: ignore[import-not-found]
    except ImportError:
        print(line, file=sys.stderr if warn else sys.stdout, flush=True)
        return
    Logger.microservice_name = os.environ.get("OPENC3_MICROSERVICE_NAME")
    (Logger.warn if warn else Logger.info)(line, scope=SCOPE)


def output_of(resource: dict[str, Any]) -> dict[str, Any]:
    """The feed output node's settings as the run published them (constellation.output)."""
    return json.loads(resource.get("constellation.output") or "{}")



def documents(
    resource: dict[str, Any],
    items: list[dict[str, Any]],
    blocks: list[str],
    index: dict[str, Any],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """The run as the Constellation Forecast view reads it: workflow-<slug>.json, and index.json
    with this workflow's entry replaced. Kept identical to publish_widget() in the Yamcs bridge
    (a test compares them)."""
    slug = resource.get("constellation.workflow") or "workflow"
    entry = {
        "workflow": slug,
        "workflow_version": resource.get("constellation.workflow_version"),
        "kind": resource.get("constellation.template") or results.kind(items),
        "run_id": resource.get("constellation.run_id"),
        "delivered_at": now.isoformat(),
        "blocks": blocks,
        "count": len(items),
    }
    workflows = {w["workflow"]: w for w in index.get("workflows", [])}
    workflows[slug] = entry
    return {
        f"workflow-{slug}.json": {**entry, "items": items},
        "index.json": {"workflows": sorted(workflows.values(), key=lambda w: w["workflow"])},
    }


def bucket() -> tuple[Any, str]:
    """COSMOS's bucket client and its config bucket, which the storage API serves to the tool."""
    from openc3.utilities.bucket import Bucket  # type: ignore[import-not-found]

    return Bucket.get_client(), os.environ["OPENC3_CONFIG_BUCKET"]


def publish_documents(
    resource: dict[str, Any], items: list[dict[str, Any]], blocks: list[str]
) -> None:
    client, name = bucket()
    found = client.get_object(bucket=name, key=f"{DOCS}/index.json")
    index = json.loads(found["Body"].read()) if found else {}
    for key, doc in documents(resource, items, blocks, index, datetime.now(UTC)).items():
        body = json.dumps(doc).encode()
        client.put_object(
            bucket=name, key=f"{DOCS}/{key}", body=body, content_type="application/json"
        )


def _minute(iso: str | None) -> str:
    return f"{iso[:16].replace('T', ' ')}Z" if iso else ""





def priced_passes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items that are a plain forecast pass with expected GB: no action of any kind (advice,
    approved, held or rejected), no series."""
    return [
        i
        for i in items
        if (i.get("entity") or {}).get("type") == "pass"
        and results.expected_gb(i) is not None
        and results.action_state(i) is None
        and not i.get("series")
    ]


def swap_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items advising a rain-fade swap, or approved in Constellation, with the deadline to
    decide by: never one held or rejected, even when its action is sent with it. One with no
    expected GB is logged and skipped: it can never be shown, and raising on it would retry the
    run forever."""
    swaps = [
        i
        for i in items
        if results.action_state(i) in ("advice", "approved")
        and (i.get("action") or {}).get("verb") == "swap"
        and i["action"].get("deadline")
    ]
    shown = [i for i in swaps if results.expected_gb(i) is not None]
    if len(shown) < len(swaps):
        log(f"skipped {len(swaps) - len(shown)} swap(s) with no expected GB in the item")
    return shown


def held_until(item: dict[str, Any]) -> str:
    """When a held action can no longer be approved, as the API's expires_at() has it: its cancel
    deadline, else when its pass starts, or a link's span ends. The API sends a held item without
    its action, so mostly the latter."""
    end = item["until"] if (item.get("entity") or {}).get("type") == "link" else item["target_at"]
    return (item.get("action") or {}).get("deadline") or end


# With no pass ahead, NEXT_PASS says so rather than keep the last run's pass.
NO_PASS: dict[str, Any] = {
    "SATELLITE": "NONE",
    "ANTENNA": "NONE",
    "AOS": "",
    "LOS": "",
    "EXPECTED_GB_P10": 0.0,
    "EXPECTED_GB_P50": 0.0,
    "EXPECTED_GB_P90": 0.0,
    "MAX_ELEVATION": 0.0,
}

# The same for CAPACITY and MODCOD, with no antenna-day or no plan ahead.
NO_CAPACITY: dict[str, Any] = {
    "DAY": "NONE",
    "ANTENNAS": 0,
    "PASSES": 0,
    "SELLABLE_MIN": 0.0,
    "GB_P10": 0.0,
    "GB_P50": 0.0,
    "TIGHTEST_ANTENNA": "NONE",
    "TIGHTEST_GB_P50": 0.0,
}
NO_MODCOD: dict[str, Any] = {
    "SATELLITE": "NONE",
    "ANTENNA": "NONE",
    "AOS": "",
    "CHANGES": 0,
    "FIRST": "NONE",
    "LAST": "NONE",
    "MIN_SINR_DB": 0.0,
    "PLANNED_PASSES": 0,
}

# Every SWAP item is written each time, so a cleared swap never leaves the last one's values
# behind. With nothing to decide, the minutes left sit far above the limits: green, not red.
NO_SWAP: dict[str, Any] = {
    "OPEN": 0,
    "SATELLITE": "NONE",
    "ANTENNA": "NONE",
    "AOS": "",
    "KEEPS_GB_P10": 0.0,
    "EXPECTED_GB_P50": 0.0,
    "BACKUP_ANTENNA": "NONE",
    "BACKUP_AOS": "",
    "DEADLINE": "",
    "MINUTES_TO_DEADLINE": 99999.0,
    "STATUS": "NONE",
    "APPROVED": 0,
    "PENDING": 0,
}


def capacity_packet(items: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """CAPACITY: one day's capacity across the antennas (today's, else the next day forecast),
    and the antenna with the least of it. None without antenna-day items."""
    days = [
        i
        for i in items
        if (i.get("entity") or {}).get("type") == "antenna" and "p50" in (i.get("value") or {})
    ]
    if not days:
        return None
    today = now.date().isoformat()
    dates = sorted({d["target_at"][:10] for d in days})
    day = today if today in dates else next((d for d in dates if d > today), dates[-1])
    rows = [d for d in days if d["target_at"][:10] == day]
    tightest = min(rows, key=lambda d: d["value"]["p50"])
    # A run that forecasts p50 only has no wet tail: 0, as the packet field cannot be empty.
    wet_tail = all(isinstance(r["value"].get("p10"), int | float) for r in rows)
    return {
        "DAY": day,
        "ANTENNAS": len(rows),
        "PASSES": sum(int(r["value"].get("passes") or 0) for r in rows),
        "SELLABLE_MIN": round(sum(float(r["value"].get("sellable_min") or 0) for r in rows), 1),
        "GB_P10": round(sum(r["value"]["p10"] for r in rows), 3) if wet_tail else 0.0,
        "GB_P50": round(sum(r["value"]["p50"] for r in rows), 3),
        "TIGHTEST_ANTENNA": tightest["entity"]["id"],
        "TIGHTEST_GB_P50": round(tightest["value"]["p50"], 3),
    }


def modcod_plans(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The passes with a MODCOD schedule: each change of MODCOD over the pass, and when."""
    return [
        i for i in items if any("modcod" in (s.get("value") or {}) for s in i.get("series") or [])
    ]


def modcod_packet(items: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """MODCOD: the next pass's MODCOD plan, its changes and its lowest SINR. None without a plan
    still to fly."""
    plans = sorted(
        (p for p in modcod_plans(items) if datetime.fromisoformat(p["until"]) > now),
        key=lambda p: p["target_at"],
    )
    if not plans:
        return None
    nxt = plans[0]
    steps = nxt["series"]
    sinr = [
        s["value"]["sinr_db"] for s in steps if isinstance(s["value"].get("sinr_db"), int | float)
    ]
    return {
        "SATELLITE": nxt["entity"]["satellite_id"],
        "ANTENNA": nxt["entity"]["antenna_id"],
        "AOS": _minute(nxt["target_at"]),
        "CHANGES": len(steps) - 1,
        "FIRST": str(steps[0]["value"]["modcod"]),
        "LAST": str(steps[-1]["value"]["modcod"]),
        "MIN_SINR_DB": round(min(sinr), 2) if sinr else 0.0,
        "PLANNED_PASSES": len(plans),
    }


def packets(
    passes: list[dict[str, Any]], swaps: list[dict[str, Any]], now: datetime, pending: int = 0
) -> dict[str, dict[str, Any]]:
    """The CONSTELLATION packets for what is forecast now, from spec items: values only, ready to
    inject. `pending` is how many swaps the newest swap run holds for approval in Constellation."""
    upcoming = sorted(
        (p for p in passes if datetime.fromisoformat(p["until"]) > now),
        key=lambda p: p["target_at"],
    )
    day = [
        p for p in upcoming if datetime.fromisoformat(p["target_at"]) < now + timedelta(hours=24)
    ]
    open_swaps = sorted(
        (s for s in swaps if datetime.fromisoformat(s["action"]["deadline"]) > now),
        key=lambda s: s["action"]["deadline"],
    )
    out: dict[str, dict[str, Any]] = {
        "NEXT_24H": {
            "PASSES": len(day),
            "EXPECTED_GB_P10": round(sum(results.expected_gb(p)["p10"] for p in day), 3),
            "EXPECTED_GB_P50": round(sum(results.expected_gb(p)["p50"] for p in day), 3),
        },
        "NEXT_PASS": NO_PASS,
        "SWAP": {**NO_SWAP, "PENDING": pending},
    }
    # The next AOS, as the Forecast tool shows it: a pass already under way counts in the day only.
    nxt = next((p for p in upcoming if datetime.fromisoformat(p["target_at"]) > now), None)
    if nxt:
        gb = results.expected_gb(nxt)
        elevation = results.max_elevation(nxt)
        out["NEXT_PASS"] = {
            "SATELLITE": nxt["entity"]["satellite_id"],
            "ANTENNA": nxt["entity"]["antenna_id"],
            "AOS": _minute(nxt["target_at"]),
            "LOS": _minute(nxt["until"]),
            "EXPECTED_GB_P10": gb["p10"],
            "EXPECTED_GB_P50": gb["p50"],
            "EXPECTED_GB_P90": gb["p90"],
            "MAX_ELEVATION": float(elevation or 0.0),
        }
    if open_swaps:
        swap = open_swaps[0]
        action, gb = swap["action"], results.expected_gb(swap)
        deadline = datetime.fromisoformat(action["deadline"])
        out["SWAP"] = {
            "OPEN": len(open_swaps),
            "SATELLITE": swap["entity"]["satellite_id"],
            "ANTENNA": swap["entity"]["antenna_id"],
            "AOS": _minute(swap["target_at"]),
            "KEEPS_GB_P10": gb["p10"],
            "EXPECTED_GB_P50": gb["p50"],
            "BACKUP_ANTENNA": action.get("to_antenna_id") or "NONE",
            "BACKUP_AOS": _minute(action.get("to_at")),
            "DEADLINE": _minute(action["deadline"]),
            "MINUTES_TO_DEADLINE": round((deadline - now).total_seconds() / 60, 1),
            "STATUS": "APPROVED" if results.action_state(swap) == "approved" else "ADVISED",
            "APPROVED": sum(results.action_state(s) == "approved" for s in open_swaps),
            "PENDING": pending,
        }
    return out


class Forecast:
    """The newest answer of each kind of workflow: a run replaces what the last run of its kind
    said, empty or not (a swap run advising nothing takes the swap down), and never another
    kind's. Which kind is the run's own constellation.template, else read off its items. A kind
    no run has brought is None, and its packets are not injected: COSMOS shows no value rather
    than NONE and 0, which would read as a forecast of nothing."""

    def __init__(self) -> None:
        self.passes: list[dict[str, Any]] | None = None
        self.swaps: list[dict[str, Any]] | None = None
        # When each swap the newest swap run holds for approval in Constellation can no longer
        # be approved (held_until): counted until then, not shown. None only in a 0.8.0 state.
        self.held: list[str | None] = []
        self.capacity: list[dict[str, Any]] | None = None
        self.plans: list[dict[str, Any]] | None = None
        self.run: dict[str, Any] = {}
        # Approvals already reported carried out, by decision: each run shows them again.
        self.acted: dict[str, str] = {}

    def take(self, resource: dict[str, Any], items: list[dict[str, Any]]) -> int:
        answers = resource.get("constellation.template") or results.kind(items)
        if answers == "rain-fade-swap":
            self.swaps = swap_items(items)
            self.held = [held_until(i) for i in items if results.action_state(i) == "pending"]
        elif answers == "antenna-capacity":
            self.capacity = items
        elif answers == "modcod-plan":
            self.plans = modcod_plans(items)
        elif answers == "pass-yield":
            self.passes = priced_passes(items)
        self.run = {
            "WORKFLOW": resource.get("constellation.workflow") or "",
            "TEMPLATE": answers,
            "WORKFLOW_VERSION": int(resource.get("constellation.workflow_version") or 0),
            "RUN_ID": resource.get("constellation.run_id") or "",
            "RESULTS": len(items),
        }
        return len(items)

    def saved(self) -> dict[str, Any]:
        return dict(vars(self))

    def restore(self, saved: dict[str, Any]) -> None:
        saved = dict(saved)
        # A state kept before deadlines were (0.8.0) has only the count.
        count = saved.pop("pending", 0)
        vars(self).update(saved)
        if "held" not in saved:
            self.held = [None] * int(count or 0)

    def publish(self, now: datetime) -> int:
        pending = sum(d is None or datetime.fromisoformat(d) > now for d in self.held)
        out = packets(self.passes or [], self.swaps or [], now, pending)
        if self.passes is None:
            del out["NEXT_PASS"], out["NEXT_24H"]
        if self.swaps is None:
            del out["SWAP"]
        if self.capacity is not None:
            out["CAPACITY"] = capacity_packet(self.capacity, now) or NO_CAPACITY
        if self.plans is not None:
            out["MODCOD"] = modcod_packet(self.plans, now) or NO_MODCOD
        if self.run:
            out["RUN"] = self.run
        for packet, values in out.items():
            inject(packet, fit(values))
        return len(out)


def check_in(consumer: str) -> bool:
    """Whether Constellation took the check-in; one that failed is tried again next poll. A
    refused key or plan (401, 403) is raised, as the bridge stops polling on it."""
    try:
        call(
            "POST",
            "/predictions/feed/checkin",
            {
                "consumer": consumer,
                "system": "cosmos",
                "target": f"OpenC3 COSMOS target {TARGET}",
                "bridge_version": VERSION,
                # The feed output it reads: only the widget layout, from mapping version 2.
                "mapping_versions": [2],
                "can": {"events": False, "timeline": False, "parameters": True, "buckets": True},
                # The Constellation Forecast tool ships in this plugin with the bridge.
                "widget": {"installed": True, "version": VERSION},
            },
        )
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
            raise
        # An outage is said by the poll that follows.
        if not isinstance(exc, OSError) or (
            isinstance(exc, urllib.error.HTTPError) and exc.code < 500
        ):
            log(f"could not check in ({exc})", warn=True)
        return False
    return True


def report(consumer: str, cursor: str, applied: dict[str, int]) -> None:
    try:
        results.report(call, consumer, cursor, applied, f"OpenC3 COSMOS target {TARGET}")
    except Exception as exc:
        log(f"could not report ({exc})", warn=True)


def api_origin() -> str:
    return results.origin(os.environ["CONSTELLATION_API_URL"])


def state_key(consumer: str) -> str:
    return f"{DOCS}/bridge-{consumer}.json"


def resume(consumer: str, forecast: Forecast) -> str:
    """The cursor to read on from: the kept one, with the forecast kept beside it, else a first
    start's."""
    client, name = bucket()
    found = client.get_object(bucket=name, key=state_key(consumer))
    saved = json.loads(found["Body"].read()) if found else {}
    # A cursor is the feed's own: one kept for another API means nothing here.
    if saved.get("api", api_origin()) != api_origin():
        log(f"the state kept for {saved['api']} is not for this API: starting afresh")
        saved = {}
    if not saved:
        cursor = results.start_cursor(call, consumer)
        log(f"first start, reading after {cursor!r}")
        return cursor
    forecast.restore(saved["forecast"])
    log(f"resuming after {saved['cursor']!r}")
    return saved["cursor"]


def keep(consumer: str, cursor: str, forecast: Forecast) -> None:
    client, name = bucket()
    kept = {"cursor": cursor, "forecast": forecast.saved(), "api": api_origin()}
    body = json.dumps(kept).encode()
    client.put_object(
        bucket=name, key=state_key(consumer), body=body, content_type="application/json"
    )


def main() -> None:
    consumer = os.environ.get("CONSUMER", "cosmos-bridge")
    forecast, refreshed, checked_in, refused, held = Forecast(), 0.0, "", "", ""
    # Why Constellation could not be read, said once until it can be again.
    failing = ""
    hinted = False
    checked_at = 0.0
    cursor: str | None = None

    def pause(seconds: float) -> None:
        """Waits `seconds`, refreshing the packets at least every POLL_S meanwhile, whatever the
        wait is for (a poll, a missing or refused key, a hold-off): the forecast held is the
        newest there is, and the next pass and the minutes left to a swap move with the clock,
        not only with runs."""
        nonlocal refreshed
        while seconds > 0:
            if time.time() - refreshed > REFRESH_S and forecast.run:
                try:
                    forecast.publish(datetime.now(UTC))
                    refreshed = time.time()
                except Exception as exc:
                    log(f"could not refresh the packets ({type(exc).__name__}: {exc})", warn=True)
            time.sleep(min(seconds, POLL_S))
            seconds -= POLL_S

    if why := results.key_refusal(os.environ["CONSTELLATION_API_URL"]):
        log(why, warn=True)
        sys.exit(2)
    log(f"feed -> COSMOS {TARGET} as {consumer}")
    while True:
        key = api_key()
        if not key:
            log(
                "No CONSTELLATION_API_KEY in Admin > Secrets yet: add a Constellation key with"
                f" predictions:read there ({results.site()}/keys)",
                warn=True,
            )
            pause(KEY_WAIT_S)
            continue
        if key == refused:
            pause(KEY_WAIT_S)
            continue
        failed = ""
        try:
            stale = time.time() - checked_at > results.CHECKIN_S
            if (key != checked_in or stale) and check_in(consumer):
                checked_in, checked_at = key, time.time()
            if cursor is None:
                cursor = resume(consumer, forecast)
            page = results.results_page(call, consumer, cursor)
            held = ""
            if failing:
                log("reached Constellation again")
                failing = ""
            if not (forecast.run or page["entries"] or hinted):
                log(
                    f"no run for {consumer} yet. In Constellation, send a workflow to the OpenC3"
                    f" COSMOS output with connection {consumer}"
                )
                hinted = True
            if lost := results.expired(page, cursor):
                log(lost, warn=True)
            for entry in page["entries"]:
                entry = results.whole(entry, lambda c, n: results.rest_of_run(call, consumer, c, n))
                parts = results.unpack(entry["otlp"], consumer, log)
                if not parts:
                    cursor = entry["cursor"]
                    keep(consumer, cursor, forecast)
                    continue
                applied = {"parameters": 0, "forecasts": 0}
                for resource, items in parts:
                    system = resource.get("constellation.system")
                    if system != "cosmos":
                        log(
                            f"refused an output of run {resource.get('constellation.run_id')}: it"
                            f" is a {system} output, not a COSMOS one"
                        )
                        applied["refused"] = applied.get("refused", 0) + 1
                        continue
                    # One malformed item is skipped, logged and counted, never worth the runs
                    # after it: raising on it would retry this run forever.
                    good = results.well_formed(items, resource.get("constellation.run_id"), log)
                    if bad := len(items) - len(good):
                        applied["malformed"] = applied.get("malformed", 0) + bad
                    items = good
                    applied["forecasts"] += forecast.take(resource, items)
                    # Approved swaps it shows as instructions: the platform shows them carried out.
                    if acted := results.first_acted(swap_items(items), forecast.acted):
                        applied["acted"] = applied.get("acted", 0) + acted
                    widget = output_of(resource).get("widget") or {}
                    if widget.get("on"):
                        publish_documents(resource, items, widget["blocks"])
                        applied["widget_updates"] = applied.get("widget_updates", 0) + 1
                applied["parameters"] = forecast.publish(datetime.now(UTC))
                refreshed = time.time()
                # Only a run applied moves the cursor: one that failed part-way is retried.
                cursor = entry["cursor"]
                report(consumer, cursor, applied)
                keep(consumer, cursor, forecast)
                log(f"injected {applied['parameters']} packets, {cursor}")
            if page["entries"]:
                continue  # catching up on the runs since the cursor: the next one now
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if why := results.refusal(exc.code, body):
                log(
                    f"{why}. Stopped polling until the key in Admin > Secrets changes or the"
                    " plugin restarts",
                    warn=True,
                )
                refused = key
                pause(KEY_WAIT_S)
                continue
            # Locked out, rate limited or out of plan: said once and waited out, never hammered.
            if hold := results.held_off(exc.code, body):
                why, wait = hold
                if why != held:
                    log(f"{why}. Asking again in {wait:.0f} s", warn=True)
                    held = why
                pause(wait)
                continue
            failed = f"Constellation answered HTTP {exc.code}"
        except OSError as exc:  # unreachable, refused, timed out (URLError is one)
            failed = f"could not reach Constellation ({getattr(exc, 'reason', exc)})"
        except Exception as exc:  # a bad poll is logged and retried, never fatal
            log(f"{type(exc).__name__}: {exc}", warn=True)
        if failed:
            if failed != failing:
                log(
                    f"{failed}. Showing the forecast held and asking again every {POLL_S} s",
                    warn=True,
                )
            failing = failed
        pause(POLL_S)


if __name__ == "__main__":
    main()
