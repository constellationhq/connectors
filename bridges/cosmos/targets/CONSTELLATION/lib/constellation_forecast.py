"""Constellation's forecasts in COSMOS scripts. In Script Runner (Python):

    load_utility("CONSTELLATION/lib/constellation_forecast.py")

    for p in forecast_passes(satellite="sat-2", hours=12):
        print(p["aos"], p["antenna"], p["expected_gb"])

    nxt = next_forecast_pass("sat-2")              # the next one, or None
    plan = modcod_plan("sat-2")                    # its MODCOD steps, to command the modem by
    swap = held_swap()                             # the soonest swap waiting on a decision
    approve_held_swap()                            # with the plugin's approvals on

It reads what the plugin's bridge writes into COSMOS (<scope>/constellation/ in the config
bucket, the documents the Constellation Forecast tool draws) through COSMOS's storage API, with
the script's own COSMOS sign-in: a script never holds a Constellation key. Times are timezone-aware
UTC datetimes. Every pass is a forecast from orbits and antenna masks, not a booked contact.

load_utility runs this file in Script Runner's own globals, beside COSMOS's script API and its
running-script machinery, so every name it defines is either one of the public functions below or
starts with _constellation_: a common name (_time, datetime, json) would replace COSMOS's own and
break the script. Nothing here calls a public function by name, so a script may reuse those names.
"""

import base64 as _constellation_base64
import json as _constellation_json
from contextlib import nullcontext as _constellation_nullcontext
from datetime import UTC as _constellation_utc
from datetime import datetime as _constellation_datetime
from datetime import timedelta as _constellation_timedelta

# HELD's items, as held_swap() returns them (lower case).
_constellation_held_items = [
    "WORKFLOW",
    "RUN_ID",
    "ITEM_ID",
    "SATELLITE",
    "ANTENNA",
    "AOS",
    "ADVICE",
    "BACKUP_ANTENNA",
    "BACKUP_AOS",
    "GAIN_GB",
    "DEADLINE",
    "MINUTES_TO_DEADLINE",
]


def _constellation_quietly():
    """Script Runner steps through every line a script runs, the helper's included, with its line
    delay: reading a run of a hundred passes line by line takes minutes. COSMOS's
    disable_instrumentation() runs the helper at full speed; outside Script Runner, nothing."""
    try:
        from openc3.script import disable_instrumentation  # type: ignore[import-not-found]
    except ImportError:
        return _constellation_nullcontext()
    return disable_instrumentation()


def _constellation_document(name, scope=None):
    """One of the bridge's documents (index.json, workflow-<slug>.json), or None before the bridge
    wrote it."""
    import openc3.script as script  # type: ignore[import-not-found]

    scope = scope or getattr(script, "OPENC3_SCOPE", None) or "DEFAULT"
    response = script.API_SERVER.request(
        "get",
        f"/openc3-api/storage/download_file/{scope}/constellation/{name}",
        query={"bucket": "OPENC3_CONFIG_BUCKET"},
        scope=scope,
    )
    if response is None or response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(f"COSMOS answered {response.status_code} reading constellation/{name}")
    return _constellation_json.loads(_constellation_base64.b64decode(response.json()["contents"]))


def _constellation_when(value):
    return _constellation_datetime.fromisoformat(value) if value else None


def _constellation_workflows(scope):
    return (_constellation_document("index.json", scope) or {}).get("workflows") or []


def _constellation_of_kind(kind, workflow, scope):
    if workflow:
        return _constellation_document(f"workflow-{workflow}.json", scope)
    for entry in _constellation_workflows(scope):
        if entry.get("kind") == kind:
            return _constellation_document(f"workflow-{entry['workflow']}.json", scope)
    return None


def _constellation_gb(item):
    """A pass's expected GB (p50): the ledger's expected_gb, else the item's own value. A p10 or
    p90 alongside is deprecated and never read."""
    ledger = ((item.get("explain") or {}).get("expected_gb") or {}).get("value")
    for gb in (ledger, item.get("value")):
        p50 = gb.get("p50") if isinstance(gb, dict) else None
        if isinstance(p50, int | float) and not isinstance(p50, bool):
            return p50
    return None


def _constellation_elevation(item):
    for entry in (item.get("explain") or {}).values():
        orbit = isinstance(entry, dict) and entry.get("source") == "orbit"
        if orbit and entry.get("unit") == "deg":
            return entry.get("value")
    return None


def _constellation_passes(satellite, hours, workflow, scope):
    doc = _constellation_of_kind("pass-yield", workflow, scope) or {}
    now = _constellation_datetime.now(_constellation_utc)
    until = now + _constellation_timedelta(hours=hours)
    out = []
    for item in doc.get("items") or []:
        entity = item.get("entity") or {}
        gb = _constellation_gb(item)
        if entity.get("type") != "pass" or gb is None or item.get("action") or item.get("series"):
            continue
        if satellite and entity.get("satellite_id") != satellite:
            continue
        aos = _constellation_when(item.get("target_at"))
        los = _constellation_when(item.get("until"))
        if los is None or los <= now or aos >= until:
            continue
        out.append(
            {
                "pass_id": entity.get("id"),
                "satellite": entity.get("satellite_id"),
                "ground_station": entity.get("ground_station_id"),
                "antenna": entity.get("antenna_id"),
                "aos": aos,
                "los": los,
                "expected_gb": gb,
                "max_elevation": _constellation_elevation(item),
            }
        )
    return sorted(out, key=lambda p: p["aos"])


def _constellation_held(scope):
    from openc3.script import tlm  # type: ignore[import-not-found]

    kwargs = {"scope": scope} if scope else {}
    if not tlm("CONSTELLATION HELD OPEN", **kwargs):
        return None
    return {
        name.lower(): tlm(f"CONSTELLATION HELD {name}", **kwargs)
        for name in _constellation_held_items
    }


def _constellation_decide(command, scope):
    from openc3.script import cmd  # type: ignore[import-not-found]

    swap = _constellation_held(scope)
    if swap is None:
        raise RuntimeError("Constellation holds no swap for a decision")
    kwargs = {"scope": scope} if scope else {}
    ids = f"RUN_ID '{swap['run_id']}', ITEM_ID '{swap['item_id']}'"
    cmd(f"CONSTELLATION {command} with {ids}", **kwargs)
    return swap


def forecast_workflows(scope=None):
    """Every workflow that delivers to this COSMOS: its slug, kind, run and when it arrived."""
    with _constellation_quietly():
        return _constellation_workflows(scope)


def forecast_document(workflow, scope=None):
    """The newest run of `workflow` as the bridge wrote it, items and all, or None."""
    with _constellation_quietly():
        return _constellation_document(f"workflow-{workflow}.json", scope)


def forecast_passes(satellite=None, hours=24, workflow=None, scope=None):
    """The forecast passes still to come within `hours`, soonest first, from the pass-yield
    workflow (or `workflow`): one dict each with pass_id, satellite, ground_station, antenna,
    aos, los, expected_gb (p50) and max_elevation."""
    with _constellation_quietly():
        return _constellation_passes(satellite, hours, workflow, scope)


def next_forecast_pass(satellite=None, workflow=None, scope=None):
    """The next forecast pass of `satellite` (any satellite when None) in the coming 72 hours."""
    with _constellation_quietly():
        passes = _constellation_passes(satellite, 72, workflow, scope)
    return passes[0] if passes else None


def modcod_plan(satellite=None, workflow=None, scope=None):
    """The MODCOD plans still to fly, soonest first, from the modcod-plan workflow (or
    `workflow`): pass_id, satellite, antenna, aos, los and steps, each step a dict with at,
    modcod and sinr_db, the planned MODCOD from that moment."""
    with _constellation_quietly():
        return _constellation_modcod(satellite, workflow, scope)


def _constellation_modcod(satellite, workflow, scope):
    doc = _constellation_of_kind("modcod-plan", workflow, scope) or {}
    now = _constellation_datetime.now(_constellation_utc)
    out = []
    for item in doc.get("items") or []:
        entity = item.get("entity") or {}
        series = [s for s in item.get("series") or [] if "modcod" in (s.get("value") or {})]
        if not series or (satellite and entity.get("satellite_id") != satellite):
            continue
        los = _constellation_when(item.get("until"))
        if los is not None and los <= now:
            continue
        out.append(
            {
                "pass_id": entity.get("id"),
                "satellite": entity.get("satellite_id"),
                "antenna": entity.get("antenna_id"),
                "aos": _constellation_when(item.get("target_at")),
                "los": los,
                "steps": [
                    {
                        "at": _constellation_when(s.get("t") or s.get("at")),
                        "modcod": s["value"]["modcod"],
                        "sinr_db": s["value"].get("sinr_db"),
                    }
                    for s in series
                ],
            }
        )
    return sorted(out, key=lambda p: p["aos"] or now)


def held_swap(scope=None):
    """The soonest swap Constellation holds for a decision, as CONSTELLATION HELD shows it (its
    items, lower case), or None."""
    with _constellation_quietly():
        return _constellation_held(scope)


def approve_held_swap(scope=None):
    """Approves the swap HELD shows (the APPROVE_SWAP command, so the plugin's approvals must be
    on). COSMOS asks first, as the command is hazardous. Returns the swap approved."""
    return _constellation_decide("APPROVE_SWAP", scope)


def decline_held_swap(scope=None):
    """Declines the swap HELD shows (DECLINE_SWAP). Returns the swap declined."""
    return _constellation_decide("DECLINE_SWAP", scope)
