"""Constellation workflow forecasts into OpenC3 COSMOS, run by COSMOS as a plugin microservice.

Pulls the Constellation feed outbound (GET /predictions/feed) as its own connection (CONSUMER),
reads only the part of each run a feed output node addressed to this connection (the
resourceLogs whose constellation.connection is CONSUMER; one for another system is refused and
reported, as the Yamcs and Lattice bridges do), and injects the forecast into the CONSTELLATION
target's packets (targets/CONSTELLATION/cmd_tlm/tlm.txt):

    NEXT_PASS         the next predicted pass and its expected GB, and whether it is at risk
                      as the Platform says (AT_RISK), against the contract minimum
    NEXT_PASS_<NAME>  the same for each satellite the plugin's `satellites` variable names
    NEXT_24H          passes and expected GB over the next 24 hours, and how many are at risk
    SWAP              the soonest rain-fade swap to decide, as advice (add a backup pass or move to
                      it), its backup's AOS, LOS and GB, the gain, and minutes left (limits go
                      yellow, then red)
    HELD              the soonest swap Constellation holds for a person to decide, read from
                      GET /predictions/actions, with the ids the APPROVE_SWAP and DECLINE_SWAP
                      commands take (cmd_tlm/cmd.txt, with the plugin's approvals on)
    CAPACITY          one day's capacity across the antennas, and the tightest
    MODCOD            the next pass's MODCOD plan
    RUN               the latest workflow run received, of any kind

A packet is injected only once a run of its kind has arrived; HELD once a swap run has.

So COSMOS's own Telemetry Viewer, Telemetry Grapher, Limits Monitor and Data Extractor show the
forecast with no other install. It also writes each run, and an index of workflows, as JSON into
COSMOS's config bucket (<SCOPE>/constellation/), the documents the Yamcs bridge writes to its
bucket, which the plugin's Constellation Forecast tool and its script helper
(targets/CONSTELLATION/lib/constellation_forecast.py) read through COSMOS's storage API. It
checks in and reports what it wrote, like the Yamcs bridge.
The last cursor applied and the forecast it held are kept in the same bucket
(<SCOPE>/constellation/bridge-<consumer>.json), so a restart resumes there with the same packets;
a first start applies only the newest run for this connection.
The Constellation key comes from COSMOS's secrets (Admin > Secrets), read at each poll so a key
added or changed there is used without a reinstall, and never leaves this process.

Two more things it does only when the plugin is installed with them (plugin.txt):

    outcomes  For each satellite mapped to the COSMOS interface its downlink arrives on, what each
              forecast pass delivered: the bytes COSMOS received on that interface between AOS
              and LOS, sent to Constellation as a contact record (POST /telemetry), so the audit
              and calibration see what happened without the fleet agent or a pass log. The key
              then needs telemetry:write.
    timeline  COSMOS Enterprise only: the forecast passes of the next 48 hours as reserve
              activities on a CONSTELLATION timeline, which Enterprise's Calendar draws beside the
              operator's own. On COSMOS Core the bridge says so once and makes no timeline.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from constellation_space import results

VERSION = "0.8.6"
TARGET = "CONSTELLATION"
POLL_S = int(os.environ.get("POLL_SECONDS", "15"))
# COSMOS shows a packet stale 30 s after its last value: re-inject every poll so a quiet feed
# still reads as current, and the next pass and minutes left keep moving.
REFRESH_S = 10
# How often, with no key or a refused one, it looks for a new key in Admin > Secrets.
KEY_WAIT_S = 60
# The connection names the API takes (its CONNECTION_PATTERN); any other is a 422 on every call.
CONNECTION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
SCOPE = os.environ.get("OPENC3_SCOPE", "DEFAULT")
# Where the Constellation Forecast tool (tools/constellationforecast) reads the documents.
DOCS = f"{SCOPE}/constellation"
# COSMOS's script API signs in with OPENC3_API_PASSWORD, which COSMOS gives a plugin microservice
# only as the service password; without it every call fails with "Token is required". Set here,
# on start: the first COSMOS import (writing the Forecast tool's documents) signs the client in
# with it, and the client keeps that session until sign_in_again() replaces it.
if "OPENC3_SERVICE_PASSWORD" in os.environ:
    os.environ.setdefault("OPENC3_API_PASSWORD", os.environ["OPENC3_SERVICE_PASSWORD"])
# What COSMOS answers a call made with a session it has ended. Setting or changing COSMOS's
# password ends every session, the script API's included, so without signing in again every
# inject after it fails until the plugin restarts.
SESSION_ENDED = "Token is invalid"
# How often it asks Constellation for the swaps held for a decision, and for the workspace's
# contract minimum. Neither moves often.
HELD_S = 30
MINIMUM_S = 300
# Outcomes: a pass is measured once COSMOS has a reading this long after its LOS, and given up
# when none comes within OUTCOME_GIVE_UP_S; readings and passes older than KEEP_S are dropped.
OUTCOME_GRACE_S = 30
OUTCOME_GIVE_UP_S = 1800
KEEP_S = 6 * 3600
# Timeline: forecast passes this far ahead are written, as reserve activities.
TIMELINE = "CONSTELLATION"
TIMELINE_AHEAD = timedelta(hours=48)
TIMELINE_S = 60


def pairs(text: str) -> dict[str, str]:
    """A plugin variable of `<constellation id>=<COSMOS name>` pairs, space separated, as a dict.
    A pair without `=` is said and left out."""
    out: dict[str, str] = {}
    for pair in text.split():
        left, sep, right = pair.partition("=")
        if not sep or not left or not right:
            # Read at import, before log() exists.
            print(
                f"constellation-bridge: left out {pair!r}: write each pair as"
                " <constellation id>=<COSMOS name>",
                file=sys.stderr,
                flush=True,
            )
            continue
        out[left] = right
    return out


def packet_name(name: str) -> str:
    """A COSMOS packet name for a satellite's own packet, as tlm.txt makes it: upper case,
    anything but letters, digits and underscores as an underscore."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper())


# Install-time choices (plugin.txt VARIABLEs), each empty or off unless set.
SATELLITES = pairs(os.environ.get("SATELLITES", ""))
OUTCOMES = pairs(os.environ.get("OUTCOMES", ""))
TIMELINE_ON = os.environ.get("TIMELINE", "off").strip().lower() == "on"


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
    "BACKUP_LOS": 32,
    "ADVICE": 256,
    "DEADLINE": 32,
    "STATUS": 16,
    "DAY": 16,
    "TIGHTEST_ANTENNA": 64,
    "FIRST": 32,
    "LAST": 32,
    "WORKFLOW": 64,
    "TEMPLATE": 32,
    "RUN_ID": 64,
    "ITEM_ID": 64,
    "RECEIVED": 32,
}


def fit(values: dict[str, Any]) -> dict[str, Any]:
    """The values with each string cut to its item's bytes, never mid-character."""
    return {
        k: v.encode()[: STRING_BYTES[k]].decode(errors="ignore") if k in STRING_BYTES else v
        for k, v in values.items()
    }


def signed_in(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """A COSMOS script API call. When COSMOS has ended the script API's session, it signs in
    again and the call is made once more."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if SESSION_ENDED not in str(exc) or not sign_in_again():
            raise
        return fn(*args, **kwargs)


def sign_in_again() -> bool:
    """Gives COSMOS's script API a new session: its API server proxy signs in when it is made,
    with the service password, which a change to COSMOS's own password leaves as it was. False
    on a COSMOS whose script API has no such proxy; the error then stands, as before."""
    try:
        import openc3.script as script  # type: ignore[import-not-found]
        from openc3.script.server_proxy import ApiServerProxy  # type: ignore[import-not-found]
    except ImportError:
        return False
    ended, script.API_SERVER = script.API_SERVER, ApiServerProxy()
    try:
        ended.shutdown()
    except Exception:
        pass  # the old session's connection is already of no use
    log("COSMOS ended this bridge's session (its password was set or changed); signed in again")
    return True


def inject(packet: str, values: dict[str, Any]) -> None:
    """COSMOS's inject_tlm, from whichever module this COSMOS exposes it in."""
    try:
        from openc3.script import inject_tlm  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - older COSMOS
        from openc3.api import inject_tlm  # type: ignore[import-not-found]
    signed_in(inject_tlm, TARGET, packet, values)


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


def _number(value: Any) -> float:
    """A number for a FLOAT item; 0 for none, as the item cannot be empty."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


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
    """When a held action can no longer be approved, as the API's expires_at() has it: its decide-by
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
    "CONTRACT_MIN_GB": 0.0,
    # A state item: COSMOS takes its state's name for a converted value.
    "AT_RISK": "NO",
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
    "ADVICE": "NONE",
    "BACKUP_LOS": "",
    "BACKUP_GB": 0.0,
    "GAIN_GB": 0.0,
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
    """MODCOD: the next pass's MODCOD plan, its changes and its lowest SINR at a change (the series
    holds only the changes). The next AOS, as NEXT_PASS has it: a pass under way is not next.
    None without a plan ahead."""
    plans = sorted(
        (p for p in modcod_plans(items) if datetime.fromisoformat(p["target_at"]) > now),
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


# The source a step writes for a value it could not fill (the Platform's simPages UNAVAILABLE).
UNFILLED = "unavailable:"


def at_risk(item: dict[str, Any], minimum: float | None) -> bool:
    """At risk as the Platform's Passes tab has it (planView isAtRisk): expected to deliver
    nothing, a value its pricing left unfilled, expected (p50) under the workspace's contract
    minimum when one is set, or flagged by a workflow as a warning or worse."""
    expected = results.expected_gb(item)["p50"]
    unfilled = any(
        isinstance(e, dict) and str(e.get("source") or "").startswith(UNFILLED)
        for e in (item.get("explain") or {}).values()
    )
    under = minimum is not None and minimum > 0 and expected < minimum
    flagged = item.get("severity") in ("warning", "critical")
    return expected <= 0 or unfilled or under or flagged


def pass_values(item: dict[str, Any], minimum: float | None) -> dict[str, Any]:
    """A NEXT_PASS packet's values for one forecast pass."""
    gb = results.expected_gb(item)
    return {
        "SATELLITE": item["entity"]["satellite_id"],
        "ANTENNA": item["entity"]["antenna_id"],
        "AOS": _minute(item["target_at"]),
        "LOS": _minute(item["until"]),
        "EXPECTED_GB_P10": gb["p10"],
        "EXPECTED_GB_P50": gb["p50"],
        "EXPECTED_GB_P90": gb["p90"],
        "MAX_ELEVATION": float(results.max_elevation(item) or 0.0),
        "CONTRACT_MIN_GB": float(minimum or 0.0),
        "AT_RISK": "YES" if at_risk(item, minimum) else "NO",
    }


# With nothing held for a decision the minutes left sit far above the limits: green, not red.
NO_HELD: dict[str, Any] = {
    "OPEN": 0,
    "WORKFLOW": "",
    "RUN_ID": "",
    "ITEM_ID": "",
    "SATELLITE": "NONE",
    "ANTENNA": "NONE",
    "AOS": "",
    "ADVICE": "NONE",
    "BACKUP_ANTENNA": "NONE",
    "BACKUP_AOS": "",
    "GAIN_GB": 0.0,
    "DEADLINE": "",
    "MINUTES_TO_DEADLINE": 99999.0,
}


def held_packet(waiting: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """HELD: the soonest swap Constellation holds for a person to decide, decide-by time ahead,
    as the advice it is, with the run and item ids APPROVE_SWAP and DECLINE_SWAP take."""
    open_ = sorted(
        (
            a
            for a in waiting
            if a.get("status") == "pending"
            and (a.get("action") or {}).get("verb") == "swap"
            and datetime.fromisoformat(a.get("expires_at") or a["target_at"]) > now
        ),
        key=lambda a: a.get("expires_at") or a["target_at"],
    )
    if not open_:
        return dict(NO_HELD)
    first = open_[0]
    action, entity = first["action"], first.get("entity") or {}
    deadline = datetime.fromisoformat(first.get("expires_at") or first["target_at"])
    return {
        "OPEN": len(open_),
        "WORKFLOW": first.get("workflow") or "",
        "RUN_ID": first["run_id"],
        "ITEM_ID": first["item_id"],
        "SATELLITE": entity.get("satellite_id") or "NONE",
        "ANTENNA": entity.get("antenna_id") or "NONE",
        "AOS": _minute(first.get("target_at")),
        "ADVICE": results.advice(first),
        "BACKUP_ANTENNA": action.get("to_antenna_id") or "NONE",
        "BACKUP_AOS": _minute(action.get("to_at")),
        "GAIN_GB": _number(action.get("gain_gb")),
        "DEADLINE": _minute(deadline.isoformat()),
        "MINUTES_TO_DEADLINE": round((deadline - now).total_seconds() / 60, 1),
    }


def packets(
    passes: list[dict[str, Any]],
    swaps: list[dict[str, Any]],
    now: datetime,
    pending: int = 0,
    minimum: float | None = None,
    satellites: dict[str, str] | None = None,
    waiting: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The CONSTELLATION packets for what is forecast now, from spec items: values only, ready to
    inject. `pending` is how many swaps the newest swap run holds for approval in Constellation,
    `minimum` the workspace's contract minimum in GB, `satellites` those with a NEXT_PASS packet
    of their own (Constellation id to COSMOS name), and `waiting` the actions read from
    GET /predictions/actions (None: no HELD packet)."""
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
            "AT_RISK": sum(at_risk(p, minimum) for p in day),
        },
        "NEXT_PASS": {**NO_PASS, "CONTRACT_MIN_GB": float(minimum or 0.0)},
        "SWAP": {**NO_SWAP, "PENDING": pending},
    }
    if waiting is not None:
        out["HELD"] = held_packet(waiting, now)
    # The next AOS, as the Forecast tool shows it: a pass already under way counts in the day only.
    ahead = [p for p in upcoming if datetime.fromisoformat(p["target_at"]) > now]
    if ahead:
        out["NEXT_PASS"] = pass_values(ahead[0], minimum)
    for satellite, name in (satellites or {}).items():
        own = next((p for p in ahead if p["entity"]["satellite_id"] == satellite), None)
        out[f"NEXT_PASS_{packet_name(name)}"] = (
            pass_values(own, minimum)
            if own
            else {**NO_PASS, "SATELLITE": satellite, "CONTRACT_MIN_GB": float(minimum or 0.0)}
        )
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
            "ADVICE": results.advice(swap),
            "BACKUP_LOS": _minute(action.get("to_until")),
            "BACKUP_GB": _number(action.get("to_gb")),
            "GAIN_GB": _number(action.get("gain_gb")),
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
        # The workspace's contract minimum in GB (GET /predictions/contract-minimum), None unset.
        self.minimum: float | None = None
        # The swap workflows that deliver here, and the actions they hold for a decision
        # (GET /predictions/actions), for HELD.
        self.swap_workflows: list[str] = []
        self.waiting: list[dict[str, Any]] = []

    def take(self, resource: dict[str, Any], items: list[dict[str, Any]]) -> int:
        answers = resource.get("constellation.template") or results.kind(items)
        if answers == "rain-fade-swap":
            self.swaps = swap_items(items)
            self.held = [held_until(i) for i in items if results.action_state(i) == "pending"]
            slug = resource.get("constellation.workflow")
            if slug and slug not in self.swap_workflows:
                self.swap_workflows.append(slug)
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
            # Re-injected every minute with the rest, so COSMOS's own received time of RUN is
            # the last refresh: this is when the run itself arrived.
            "RECEIVED": _minute(datetime.now(UTC).isoformat()),
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
        out = packets(
            self.passes or [],
            self.swaps or [],
            now,
            pending,
            self.minimum,
            SATELLITES,
            self.waiting if self.swap_workflows else None,
        )
        if self.passes is None:
            for name in [n for n in out if n == "NEXT_24H" or n.startswith("NEXT_PASS")]:
                del out[name]
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


def note(said: set[str], message: str) -> None:
    """Logs `message` as a warning once: a problem that repeats every poll is said the first time."""
    if message not in said:
        said.add(message)
        log(message, warn=True)


def describe(exc: Exception) -> str:
    """An error in a few words: an HTTP status with the API's error code, else the exception."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            answer = json.loads(exc.read())
        except ValueError:
            answer = None
        detail = answer.get("detail", answer) if isinstance(answer, dict) else None
        code = detail.get("error") if isinstance(detail, dict) else None
        return f"HTTP {exc.code}{f' {code}' if code else ''}"
    return f"{type(exc).__name__}: {exc}"


def read_waiting(forecast: Forecast, said: set[str]) -> None:
    """The actions each swap workflow delivering here holds for a decision (GET
    /predictions/actions, which the bridge's predictions:read key may read). A workflow the API
    no longer knows is skipped; any other failure keeps what was read before and is said once."""
    waiting: list[dict[str, Any]] = []
    for slug in forecast.swap_workflows:
        query = urllib.parse.urlencode({"workflow": slug})
        try:
            answer = call("GET", f"/predictions/actions?{query}") or {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            note(said, f"could not read the swaps {slug} holds ({describe(exc)}); HELD is kept")
            return
        except Exception as exc:
            note(said, f"could not read the swaps {slug} holds ({describe(exc)}); HELD is kept")
            return
        waiting += [{**a, "workflow": slug} for a in answer.get("actions") or []]
    forecast.waiting = waiting


def read_minimum(forecast: Forecast, said: set[str]) -> None:
    """The workspace's contract minimum (GET /predictions/contract-minimum), which AT_RISK reads
    each pass's low case against. Unset, or not served here, AT_RISK counts only 0 GB low cases."""
    try:
        answer = call("GET", "/predictions/contract-minimum") or {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            forecast.minimum = None
            return
        note(said, f"could not read the contract minimum ({describe(exc)}); AT_RISK is kept")
        return
    except Exception as exc:
        note(said, f"could not read the contract minimum ({describe(exc)}); AT_RISK is kept")
        return
    value = answer.get("min_gb")
    usable = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    forecast.minimum = float(value) if usable else None


class Delivered:
    """What each forecast pass of a mapped satellite delivered, measured in COSMOS: the bytes
    COSMOS received on the satellite's interface between the pass's AOS and LOS, from the
    interface's rxbytes counter read every poll. A pass that brought bytes is sent to
    Constellation as a contact record (POST /telemetry, telemetry:write). One that brought none
    is not sent: the bridge cannot tell a pass nobody booked from one that failed. Two passes of
    one satellite at the same time cannot be told apart either, so neither is sent."""

    def __init__(self, interfaces: dict[str, str]) -> None:
        self.interfaces = interfaces
        # interface -> [[unix time, rxbytes], ...], the last KEEP_S of them; not kept over a
        # restart, so a pass the bridge did not see begin is never measured.
        self.readings: dict[str, list[list[float]]] = {}
        # pass id -> the pass, from the forecast, until it is measured or given up.
        self.watch: dict[str, dict[str, Any]] = {}
        # pass id -> when its record was sent, kept 7 days so no pass is sent twice.
        self.sent: dict[str, float] = {}
        # The key Constellation refused telemetry:write for; sending waits for another.
        self.refused = ""

    def saved(self) -> dict[str, Any]:
        return {"watch": self.watch, "sent": self.sent}

    def restore(self, saved: dict[str, Any]) -> None:
        self.watch = dict(saved.get("watch") or {})
        self.sent = dict(saved.get("sent") or {})

    def follow(self, passes: list[dict[str, Any]], now: datetime) -> None:
        """Watches each forecast pass of a mapped satellite not yet sent. A watched pass the
        newest run no longer has, but which overlaps one it does have for the same satellite, is
        that pass forecast again (moved, or under a new id) and gives way to it: kept, the two
        would read as two passes at once and neither would ever be sent."""
        current: dict[str, dict[str, Any]] = {}
        for p in passes:
            entity = p["entity"]
            if entity.get("satellite_id") not in self.interfaces or entity["id"] in self.sent:
                continue
            if datetime.fromisoformat(p["until"]) < now - timedelta(seconds=KEEP_S):
                continue
            current[entity["id"]] = {
                "satellite_id": entity["satellite_id"],
                "ground_station_id": entity.get("ground_station_id"),
                "antenna_id": entity.get("antenna_id"),
                "aos": p["target_at"],
                "los": p["until"],
            }

        def span(w: dict[str, Any]) -> tuple[datetime, datetime]:
            return datetime.fromisoformat(w["aos"]), datetime.fromisoformat(w["los"])

        for pid, old in list(self.watch.items()):
            if pid in current:
                continue
            start, end = span(old)
            if any(
                new["satellite_id"] == old["satellite_id"]
                and span(new)[0] < end
                and span(new)[1] > start
                for new in current.values()
            ):
                del self.watch[pid]
        self.watch.update(current)

    def read(self, now_s: float, rxbytes: Callable[[str], int]) -> None:
        """One reading of each mapped interface's received-bytes counter."""
        for name in sorted(set(self.interfaces.values())):
            series = self.readings.setdefault(name, [])
            series.append([now_s, int(rxbytes(name))])
            while series and series[0][0] < now_s - KEEP_S:
                series.pop(0)

    def due(self, now: datetime) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
        """The contact records measurable now, by pass id, and the passes given up with why.
        Neither changes what is watched: drop() and mark_sent() do."""
        now_s = now.timestamp()
        spans = {
            pid: (
                datetime.fromisoformat(p["aos"]).timestamp(),
                datetime.fromisoformat(p["los"]).timestamp(),
            )
            for pid, p in self.watch.items()
        }
        records: list[tuple[str, dict[str, Any]]] = []
        dropped: dict[str, str] = {}
        for pid, p in self.watch.items():
            aos, los = spans[pid]
            if now_s < los + OUTCOME_GRACE_S:
                continue
            interface = self.interfaces[p["satellite_id"]]
            series = self.readings.get(interface, [])
            before = [r for r in series if r[0] <= aos]
            after = [r for r in series if r[0] >= los]
            together = [
                o
                for o, q in self.watch.items()
                if o != pid
                and q["satellite_id"] == p["satellite_id"]
                and spans[o][0] < los
                and spans[o][1] > aos
            ]
            if together:
                dropped[pid] = f"it overlaps {together[0]}, so {interface}'s bytes cannot be split"
            elif not before:
                dropped[pid] = f"the bridge was not reading {interface} at its AOS"
            elif not after:
                if now_s > los + OUTCOME_GIVE_UP_S:
                    dropped[pid] = f"no reading of {interface} came after its LOS"
            elif after[0][1] < before[-1][1]:
                dropped[pid] = f"{interface}'s counter went back (it reconnected) during the pass"
            elif after[0][1] == before[-1][1]:
                dropped[pid] = (
                    f"no bytes arrived on {interface}; not sent, as a pass nobody booked looks"
                    " the same"
                )
            else:
                record = {
                    "time": p["aos"],
                    "ground_station_id": p["ground_station_id"],
                    "ground_antenna_id": p["antenna_id"],
                    "satellite_id": p["satellite_id"],
                    "contact_id": pid[:128],
                    "fields": {
                        "bytes_delivered": int(after[0][1] - before[-1][1]),
                        "contact_status": "ok",
                        "los_utc": p["los"],
                    },
                }
                records.append((pid, record))
        return records, dropped

    def drop(self, pids: dict[str, str]) -> None:
        for pid in pids:
            self.watch.pop(pid, None)

    def mark_sent(self, pids: list[str], now_s: float) -> None:
        for pid in pids:
            self.watch.pop(pid, None)
            self.sent[pid] = now_s
        for pid, at in list(self.sent.items()):
            if at < now_s - 7 * 86400:
                del self.sent[pid]


def rxbytes(interface: str) -> int:
    """What COSMOS has received on `interface` since it connected, in bytes."""
    from openc3.script import get_interface  # type: ignore[import-not-found]

    return int(signed_in(get_interface, interface)["rxbytes"])


def measure_delivered(delivered: Delivered, forecast: Forecast, key: str, said: set[str]) -> bool:
    """Reads the mapped interfaces, then sends the passes that ended. True when what is watched or
    sent changed, so the caller keeps it."""
    now = datetime.now(UTC)
    delivered.follow(forecast.passes or [], now)
    try:
        delivered.read(now.timestamp(), rxbytes)
    except Exception as exc:
        note(said, f"could not read the outcomes' interfaces ({describe(exc)})")
        return False
    records, dropped = delivered.due(now)
    for pid, why in dropped.items():
        log(f"no outcome for {pid}: {why}")
    delivered.drop(dropped)
    if not records or key == delivered.refused:
        return bool(dropped)
    try:
        answer = call("POST", "/telemetry", [r for _, r in records]) or {}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code in (401, 403):
            delivered.refused = key
            why = results.refusal(exc.code, body, "predictions:read and telemetry:write")
            note(said, f"outcomes not sent: {why}")
        else:
            note(said, f"could not send outcomes (HTTP {exc.code}); trying again next poll")
        return bool(dropped)
    except Exception as exc:
        note(said, f"could not send outcomes ({describe(exc)}); trying again next poll")
        return bool(dropped)
    rejected = set(answer.get("rejected_indices") or [])
    for problem in answer.get("rejections") or []:
        log(f"Constellation refused outcome {records[problem['index']][0]}: {problem}", warn=True)
    delivered.mark_sent([pid for pid, _ in records], now.timestamp())
    for n, (pid, record) in enumerate(records):
        if n not in rejected:
            gb = record["fields"]["bytes_delivered"] / 1e9
            log(f"outcome {pid}: {gb:.3f} GB on {delivered.interfaces[record['satellite_id']]}")
    return True


class CoreHasNoTimelines(Exception):
    """COSMOS Core runs no timeline worker. Its API still makes a timeline, and COSMOS then starts
    the timeline's worker, which lives in Enterprise: it dies and is started again every few
    seconds for as long as the timeline is there."""


def enterprise() -> bool:
    """Whether this COSMOS is Enterprise, as its /openc3-api/info says."""
    from openc3 import script  # type: ignore[import-not-found]

    # A lambda, not the bound method: signing in again replaces script.API_SERVER.
    response = signed_in(lambda: script.API_SERVER.request("get", "/openc3-api/info"))
    if response is None or response.status_code != 200:
        status = "no answer" if response is None else response.status_code
        raise RuntimeError(f"COSMOS answered {status} to /openc3-api/info")
    return response.json().get("enterprise") is True


class Timeline:
    """The forecast passes of the next 48 hours as reserve activities on COSMOS Enterprise's
    CONSTELLATION timeline: forecasts beside the operator's own timelines, never a command or a
    script. Each activity carries its pass id; a rerun moves or removes the bridge's own
    activities only, and one that has started is left as history."""

    def __init__(self) -> None:
        self.made = False
        self.wanted = True  # a run was applied since the last write
        self.at = 0.0

    def sync(self, passes: list[dict[str, Any]], now: datetime) -> tuple[int, int]:
        """Writes the timeline; returns (added, removed). Raises CoreHasNoTimelines on COSMOS
        Core, before making anything."""
        from openc3 import script  # type: ignore[import-not-found]

        if not self.made:
            if not enterprise():
                raise CoreHasNoTimelines
            names = [t.get("name") for t in signed_in(script.list_timelines) or []]
            if TIMELINE not in names:
                signed_in(script.create_timeline, TIMELINE, color="#ff8f45")
            self.made = True
        start, stop = now + timedelta(seconds=30), now + TIMELINE_AHEAD
        wanted: dict[str, tuple[datetime, datetime, dict[str, Any]]] = {}
        for p in passes:
            aos, los = datetime.fromisoformat(p["target_at"]), datetime.fromisoformat(p["until"])
            if not start < aos < stop:
                continue
            gb = results.expected_gb(p)
            entity = p["entity"]
            name = SATELLITES.get(entity["satellite_id"], entity["satellite_id"])
            wanted[entity["id"]] = (
                aos,
                los,
                {
                    "source": "constellation",
                    "pass_id": entity["id"],
                    "satellite": entity["satellite_id"],
                    "antenna": entity["antenna_id"],
                    "forecast": (
                        f"Forecast, not a booked contact: {name} at {entity['antenna_id']},"
                        f" {gb['p50']:.1f} GB expected"
                    ),
                },
            )
        have = signed_in(
            script.get_timeline_activities,
            TIMELINE,
            start=int(start.timestamp()),
            stop=int(stop.timestamp()),
        )
        removed = 0
        kept: set[str] = set()
        for activity in have or []:
            data = activity.get("data") or {}
            if data.get("source") != "constellation":
                continue
            pid = data.get("pass_id")
            same = pid in wanted and (
                int(activity["start"]) == int(wanted[pid][0].timestamp())
                and int(activity["stop"]) == int(wanted[pid][1].timestamp())
            )
            if same and pid not in kept:
                kept.add(pid)
                continue
            start_s, uuid = activity["start"], activity["uuid"]
            signed_in(script.delete_timeline_activity, TIMELINE, start_s, uuid)
            removed += 1
        added = 0
        for pid, (aos, los, data) in wanted.items():
            if pid not in kept:
                signed_in(script.create_timeline_activity, TIMELINE, "reserve", aos, los, data)
                added += 1
        self.wanted = False
        self.at = time.time()
        return added, removed


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
                "can": {
                    "events": False,
                    "timeline": TIMELINE_ON,
                    "parameters": True,
                    "buckets": True,
                },
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


def report(
    consumer: str, cursor: str, applied: dict[str, int], outcomes: list[dict[str, str]]
) -> None:
    try:
        target = f"OpenC3 COSMOS target {TARGET}"
        results.report(call, consumer, cursor, applied, target, outcomes, log)
    except Exception as exc:
        log(f"could not report ({exc})", warn=True)


def api_origin() -> str:
    return results.origin(os.environ["CONSTELLATION_API_URL"])


def state_key(consumer: str) -> str:
    return f"{DOCS}/bridge-{consumer}.json"


def resume(consumer: str, forecast: Forecast, delivered: Delivered | None = None) -> str:
    """The cursor to read on from: the kept one, with the forecast (and the outcomes watched and
    sent) kept beside it, else a first start's."""
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
    if delivered is not None:
        delivered.restore(saved.get("outcomes") or {})
    log(f"resuming after {saved['cursor']!r}")
    return saved["cursor"]


def keep(
    consumer: str, cursor: str, forecast: Forecast, delivered: Delivered | None = None
) -> None:
    client, name = bucket()
    kept = {"cursor": cursor, "forecast": forecast.saved(), "api": api_origin()}
    if delivered is not None:
        kept["outcomes"] = delivered.saved()
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
    delivered = Delivered(OUTCOMES) if OUTCOMES else None
    timeline = Timeline() if TIMELINE_ON else None
    said: set[str] = set()
    waiting_at = minimum_at = 0.0

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

    why = results.key_refusal(os.environ["CONSTELLATION_API_URL"])
    if not CONNECTION.fullmatch(consumer):
        why = (
            f"consumer {consumer!r} is not a connection name Constellation takes: 1 to 63"
            " lowercase letters, digits and hyphens, not starting with a hyphen. Change it in"
            " the plugin's variables"
        )
    if why:
        # Said once and waited on, not exited: COSMOS respawns a microservice that exits.
        log(why, warn=True)
        while True:
            time.sleep(KEY_WAIT_S)
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
                cursor = resume(consumer, forecast, delivered)
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
                    keep(consumer, cursor, forecast, delivered)
                    continue
                applied = {"parameters": 0, "forecasts": 0}
                outcomes: list[dict[str, str]] = []
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
                    # Approved swaps it shows as approved advice: the platform shows them acted on.
                    shown = f"shown in the {TARGET} SWAP packet"
                    if acted := results.first_acted(
                        swap_items(items), forecast.acted, outcomes, shown
                    ):
                        applied["acted"] = applied.get("acted", 0) + acted
                    widget = output_of(resource).get("widget") or {}
                    if widget.get("on"):
                        publish_documents(resource, items, widget["blocks"])
                        applied["widget_updates"] = applied.get("widget_updates", 0) + 1
                applied["parameters"] = forecast.publish(datetime.now(UTC))
                refreshed = time.time()
                if timeline is not None:
                    timeline.wanted = True
                # Only a run applied moves the cursor: one that failed part-way is retried.
                cursor = entry["cursor"]
                report(consumer, cursor, applied, outcomes)
                keep(consumer, cursor, forecast, delivered)
                log(f"injected {applied['parameters']} packets, {cursor}")
            # Reads and writes beside the feed, each failing on its own: never a reason to stop it.
            now_s = time.time()
            if forecast.swap_workflows and now_s - waiting_at > HELD_S:
                read_waiting(forecast, said)
                waiting_at = now_s
            if now_s - minimum_at > MINIMUM_S:
                read_minimum(forecast, said)
                minimum_at = now_s
            if delivered is not None and measure_delivered(delivered, forecast, key, said):
                keep(consumer, cursor, forecast, delivered)
            # Until a pass-yield run arrives the passes are unknown, not none: the timeline waits.
            if (
                timeline is not None
                and forecast.passes is not None
                and timeline.wanted
                and now_s - timeline.at > TIMELINE_S
            ):
                try:
                    added, removed = timeline.sync(forecast.passes, datetime.now(UTC))
                    if added or removed:
                        log(f"timeline {TIMELINE}: {added} passes added, {removed} removed")
                except CoreHasNoTimelines:
                    timeline = None
                    log(
                        "the timeline option is on, but this COSMOS is Core, which runs no"
                        " timelines: the forecast passes stay off them (COSMOS Enterprise's"
                        " Calendar draws them)",
                        warn=True,
                    )
                except Exception as exc:
                    timeline.at = now_s
                    note(said, f"could not write the {TIMELINE} timeline ({describe(exc)})")
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
