"""Constellation workflow results into Yamcs, from inside the customer's network.

Polls the Constellation feed (GET /predictions/feed) outbound. Each run in the feed carries one
OTLP resource per feed output of its workflow, each naming the connection it is for, its items,
and the mapping that output chose: which Yamcs objects to write, what to call them, when an event
is a WARNING, and where each {token} the text uses reads from in an item. This bridge applies the
resources for its own connection (CONSUMER) and nothing else:

    events      one per item, from the mapping's message, source and severity rule; a rerun
                writes one again only for a new item or one whose event would now differ
    timeline    one item per result that spans time, in the mapping's band; the id is derived
                from the result, so a rerun updates it, and the bridge deletes its own items
                (tagged constellation-bridge) that the workflow's newest run no longer carries,
                only inside that run's window and never one that already started
    parameters  the next pass and the next 24 h under /ConstellationForecast (bridges/yamcs/widget)
    widget      the run's results and the page layout, as a JSON object in the Yamcs bucket the
                Constellation Forecast page reads (bridges/yamcs/widget)

It creates the bands and the "Constellation forecast" view it needs (CREATE_VIEWS=0 turns that
off), checks in with what it found (which Yamcs, what its account may write, whether the widget is
installed), and reports what it wrote for each run. Catching up after downtime, it applies only
the newest run of each workflow, its events stamped with the run's own time. Its state is in
STATE_DIR: CURSOR_FILE, the last cursor applied, so it resumes where it stopped, and ACTED_FILE, the
approved actions it announced, so each "Approved in Constellation" event is written once, however
often the feed sends that action again. When Yamcs lost what the bridge made (a restart without
persistent storage), it applies the newest run of every workflow again.

Yamcs credentials, when Yamcs has security on, stay here: a service account (YAMCS_CLIENT_ID and
YAMCS_CLIENT_SECRET) or a user (YAMCS_USERNAME and YAMCS_PASSWORD). They never go to Constellation.

Stdlib only, so it runs on any Python 3.12 image.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from constellation_space import results

VERSION = "0.7.0"
API = (os.environ.get("CONSTELLATION_API_URL") or "https://api.constellation.space").rstrip("/")
API_KEY = os.environ.get("CONSTELLATION_API_KEY", "").strip()
YAMCS = os.environ.get("YAMCS_URL", "http://yamcs:8090").rstrip("/")
INSTANCE = os.environ.get("YAMCS_INSTANCE", "simulator")
PROCESSOR = os.environ.get("YAMCS_PROCESSOR", "realtime")
POLL_S = int(os.environ.get("POLL_SECONDS", "15"))
CREATE_VIEWS = os.environ.get("CREATE_VIEWS", "1") != "0"
# This bridge's connection: workflows name it as an output, and it shows by this name.
CONSUMER = os.environ.get("CONSUMER", "yamcs-bridge")
SPACE = "/ConstellationForecast"
VIEW = "Constellation forecast"
BUCKET = "constellation"
WIDGET_PLUGIN = "constellation-yamcs"
MAPPING_VERSIONS = [2]
# How long a Yamcs 5xx is waited out, each time, before the call is tried again; then it is raised.
RETRY_S = (1, 2, 4)
# Tags every timeline item this bridge writes, so it never deletes one it did not make.
MADE_BY_BRIDGE = "constellation-bridge"
NAMESPACE = uuid.UUID("6f1c2a52-7d1e-4f0a-9a37-5c0b8e3d2f10")
# Where the bridge keeps its state: the image sets it to its volume, /var/lib/constellation;
# elsewhere it is the user's own state directory, which outlives a reboot as /tmp may not.
STATE_DIR = os.environ.get("STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "constellation"
)
ACTED_FILE = os.environ.get("ACTED_FILE", f"{STATE_DIR}/yamcs-bridge-acted.txt")
CURSOR_FILE = f"{STATE_DIR}/yamcs-bridge-cursor.txt"
# Each workflow's newest run applied, by cursor: what a Yamcs restart without storage lost, read
# again by cursor rather than by paging the whole feed.
NEWEST_FILE = f"{STATE_DIR}/yamcs-bridge-newest.json"

written: dict[str, int] = {}


def wrote(kind: str, n: int = 1) -> None:
    written[kind] = written.get(kind, 0) + n


def log(message: str) -> None:
    print(f"bridge: {message}", flush=True)


# --- HTTP -------------------------------------------------------------------------------------


def call(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> Any:
    return results.http(method, url, body, headers, raw, timeout=30)


# How long the bridge waits after Constellation refused it before asking again: a refusal only a
# person clears, and a key failing over and over can lock out the address (auth_lockout).
REFUSED_S = 600


class Refused(Exception):
    """Constellation refused this bridge for a reason asking again soon does not clear."""

    def __init__(self, why: str, wait: float = REFUSED_S) -> None:
        super().__init__(why)
        self.wait = wait


def refusal(exc: urllib.error.HTTPError) -> Refused | None:
    """Why Constellation refused this call, worded with the fix, or None when asking again soon
    may work (a plain rate limit, a 5xx)."""
    body = exc.read()
    if why := results.refusal(exc.code, body, api_url=API):
        return Refused(why)
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    detail = answer.get("detail", answer) if isinstance(answer, dict) else None
    error = detail.get("error") if isinstance(detail, dict) else None
    site = results.site(API)
    if exc.code == 429 and error == "auth_lockout":
        try:
            wait = max(REFUSED_S, float(exc.headers.get("Retry-After")))
        except (TypeError, ValueError):
            wait = REFUSED_S
        return Refused(
            "Constellation locked out this address after repeated refused keys (HTTP 429"
            " auth_lockout); the lockout lifts by itself, and a key that is still refused sets"
            f" it again: check that CONSTELLATION_API_KEY is a live key from {site}/keys",
            wait,
        )
    if exc.code == 429 and error == "too_many_consumers":
        return Refused(
            "this account has as many connections reading the feed as it may (HTTP 429"
            f" too_many_consumers): set CONSUMER to a connection on {site} or remove one there"
        )
    if exc.code == 402:
        return Refused(
            "Constellation needs a plan or credits for this (HTTP 402"
            f" {error or 'payment required'}): see {site}/billing"
        )
    return None


def constellation(method: str, path: str, body: Any = None) -> Any:
    """A Constellation API call; a refused key, plan or account raises Refused, with why."""
    try:
        return call(method, f"{API}{path}", body, headers={"X-API-Key": API_KEY})
    except urllib.error.HTTPError as exc:
        if refused := refusal(exc):
            raise refused from None
        raise


_token: dict[str, Any] = {}


def _login() -> str | None:
    """A Yamcs access token from this bridge's own credentials, or None with security off."""
    if os.environ.get("YAMCS_CLIENT_ID"):
        form = {
            "grant_type": "client_credentials",
            "client_id": os.environ["YAMCS_CLIENT_ID"],
            "client_secret": os.environ.get("YAMCS_CLIENT_SECRET", ""),
        }
    elif os.environ.get("YAMCS_USERNAME"):
        form = {
            "grant_type": "password",
            "username": os.environ["YAMCS_USERNAME"],
            "password": os.environ.get("YAMCS_PASSWORD", ""),
        }
    else:
        return None
    req = urllib.request.Request(
        f"{YAMCS}/auth/token",
        data=urllib.parse.urlencode(form).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read())
    _token.update(
        value=token["access_token"], until=time.time() + token.get("expires_in", 300) - 30
    )
    return _token["value"]


def yamcs(
    method: str, path: str, body: Any = None, raw: bytes | None = None, kind: str = ""
) -> Any:
    """A Yamcs API call as this bridge's account; logs in again once when the token lapsed, and
    tries a 5xx again after each of RETRY_S before raising it."""
    relogged, waits = False, list(RETRY_S)
    while True:
        token = _token["value"] if _token.get("until", 0) > time.time() else _login()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if kind:
            headers["Content-Type"] = kind
        try:
            return call(method, f"{YAMCS}/api{path}", body, headers, raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not relogged:
                _token.clear()
                relogged = True
                continue
            if exc.code in (401, 403):
                raise PermissionError(
                    f"Yamcs refused {method} {path}: this bridge's Yamcs account may not do that"
                ) from exc
            if exc.code < 500 or not waits:
                raise
            log(
                f"Yamcs answered {method} {path} with HTTP {exc.code}; trying again in {waits[0]} s"
            )
            time.sleep(waits.pop(0))


# --- the feed ---------------------------------------------------------------------------------


def runs_after(after: str) -> dict[str, Any]:
    """Up to 20 runs after cursor `after`, one record of each: enough to tell their workflows."""
    query = {"after": after, "limit": 20, "consumer": CONSUMER, "page_records": 1}
    return constellation("GET", f"/predictions/feed?{urllib.parse.urlencode(query)}")


def whole_run(cursor: str) -> dict[str, Any]:
    return results.whole(rest_of_run(cursor, 0), rest_of_run)


def rest_of_run(cursor: str, page: int) -> dict[str, Any]:
    return results.rest_of_run(constellation, CONSUMER, cursor, page)


# --- the mapping ------------------------------------------------------------------------------


def lookup(item: dict[str, Any], path: str) -> Any:
    """The value at a dotted path into an item, or None where the item has nothing there."""
    value: Any = item
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def fields(item: dict[str, Any], tokens: dict[str, str]) -> dict[str, Any]:
    """Each token a mapping's text may use, read from one item where the run's tokens table says
    it lives. `day` is the date of the time it names."""
    out = {name: lookup(item, path) for name, path in tokens.items()}
    if isinstance(out.get("day"), str):
        out["day"] = out["day"][:10]
    return out


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def render(text: str, values: dict[str, Any]) -> str:
    """The text with each {token} filled in; a token the item has no value for is empty."""

    def one(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if value is None or isinstance(value, dict | list):
            return ""
        if isinstance(value, float):
            # A share (a lock-loss risk of 0.033) keeps its digits; it never reads as 0.0.
            return f"{value:.1f}" if value == 0 or abs(value) >= 1 else f"{value:.2g}"
        if isinstance(value, str) and _ISO.match(value):
            return value[:16].replace("T", " ") + "Z"
        return str(value)

    return re.sub(r"\{([a-z0-9_.]+)\}", one, text)


def severity(events: dict[str, Any], values: dict[str, Any]) -> str:
    rule = events.get("warn_if")
    v = values.get(rule["field"]) if rule else None
    if rule and isinstance(v, int | float) and not isinstance(v, bool):
        if (v < rule["value"]) if rule["op"] == "<" else (v > rule["value"]):
            return "WARNING"
    return events["severity"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def plan(item: dict[str, Any]) -> str | None:
    """A MODCOD plan's steps, from the item's series."""
    steps = [s for s in item.get("series") or [] if (s.get("value") or {}).get("modcod")]
    return " → ".join(f"{s['t'][11:16]} {s['value']['modcod']}" for s in steps) or None


# --- writing into Yamcs -----------------------------------------------------------------------


# The last event written per item (workflow, entity and day, what the item advises): a rerun
# writes an event only for a new item or one whose event would now say something else. Timeline
# items need no such memory: their id is derived the same way, so a rerun updates them in place.
# Kept in memory, so a bridge restart writes each pass's event once more; an approved action's
# event never: it is an instruction, written once (_acted), not news to repeat.
_sent: dict[str, tuple[str, str]] = {}
_acted = results.Acted(ACTED_FILE)


# Yamcs severities a mapping may name, lowest first.
_SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


def ident(item: dict[str, Any]) -> str:
    """An item's identity across runs: its entity on its UTC day, as an antenna-capacity run
    sends one item per antenna per day."""
    return f"{item['entity']['id']}|{item['target_at'][:10]}"


def event_key(item: dict[str, Any], run: dict[str, Any]) -> str:
    verb = (item.get("action") or {}).get("verb", "")
    return f"{run['workflow']}|{ident(item)}|{verb}|{results.action_state(item) or ''}"


def send_event(
    item: dict[str, Any], events: dict[str, Any], values: dict[str, Any], run: dict[str, Any]
) -> None:
    approved = results.action_state(item) == "approved"
    if approved and item in _acted:
        wrote("unchanged")
        return
    message, level = render(events["message"], values), severity(events, values)
    if approved:
        # An instruction, not advice: said so, and never quieter than a warning.
        message = f"Approved in Constellation: {message}"
        if _SEVERITY_RANK.get(level, 0) < _SEVERITY_RANK["WARNING"]:
            level = "WARNING"
    event = {
        "message": message,
        "type": run["workflow"],
        "severity": level,
        "source": events["source"],
    }
    if run.get("made_at"):
        # The run's own time, so a catch-up after downtime does not stamp old events "now".
        event["time"] = run["made_at"]
    key, said = event_key(item, run), (event["message"], event["severity"])
    # An approval is written once by _acted (a new approval of the same swap is news again).
    if not approved and _sent.get(key) == said:
        wrote("unchanged")
        return
    yamcs("POST", f"/archive/{INSTANCE}/events", event)
    _sent[key] = said
    if approved:
        _acted.add(item)
        wrote("acted")
    wrote("events")


def timeline(
    item: dict[str, Any], tl: dict[str, Any], values: dict[str, Any], run: dict[str, Any]
) -> str:
    """Writes the item's timeline item; returns its id."""
    entity = item["entity"]
    # A closed action is no longer an instruction or a question: it loses its approved tag.
    state = None if results.past_deadline(item) else results.action_state(item)
    action = item.get("action") or {}
    item_id = str(uuid.uuid5(NAMESPACE, f"{run['workflow']}|{tl['band']}|{ident(item)}"))
    start = datetime.fromisoformat(item["target_at"])
    body = {
        "type": "EVENT",
        "name": render(tl["name"], values),
        "start": item["target_at"],
        "duration": f"{(datetime.fromisoformat(item['until']) - start).total_seconds():.0f}s",
        # A PUT replaces the whole item, so the tags go every time; the band's tag routes it.
        "tags": sorted(
            {*tl["tags"], f"band-{slug(tl['band'])}", MADE_BY_BRIDGE}
            | ({"awaiting-approval"} if state == "pending" else set())
            | ({"approved"} if state == "approved" else set())
            | ({"rejected"} if state == "rejected" else set())
        ),
        "properties": {
            k: str(v)
            for k, v in {
                "workflow": run["workflow"],
                "pass_id": entity["id"],
                "antenna_id": entity.get("antenna_id"),
                "expected_gb_p10": values.get("expected_gb.p10"),
                "expected_gb_p50": values.get("expected_gb.p50"),
                "expected_gb_p90": values.get("expected_gb.p90"),
                "plan": plan(item),
                "action_status": item.get("action_status"),
                "backup_antenna": action.get("to_antenna_id"),
                "deadline": action.get("deadline"),
                "workflow_version": item.get("workflow_version", run.get("workflow_version")),
                "run_id": run.get("run_id"),
            }.items()
            if v is not None
        },
    }
    try:
        yamcs("PUT", f"/timeline/{INSTANCE}/items/{item_id}", body)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        yamcs("POST", f"/timeline/{INSTANCE}/items", {**body, "id": item_id})
    wrote("timeline_items")
    return item_id


def prune_timeline(workflow: str, band: str, kept: set[str], window: tuple[Any, Any]) -> None:
    """Deletes this workflow's timeline items in the band that its newest run no longer carries
    (a withdrawn pass). The run is the whole answer for its window only: an item starting outside
    it, or already started, is never touched, nor is any item when the run names no window. Only
    items this bridge made, told by its tag, are ever deleted."""
    start, end = (results.parse_time(w) for w in window)
    if not start or not end:
        return
    start = max(start, results.now())
    if start >= end:
        return
    seen: set[str] = set()
    after = ""
    while True:
        window = {"start": start.isoformat(), "stop": end.isoformat()}
        query = urllib.parse.urlencode({"source": "rdb", "limit": 1000, "next": after, **window})
        found = yamcs("GET", f"/timeline/{INSTANCE}/items?{query}") or {}
        # Yamcs 5.14 can answer the same items with a new continuationToken for good.
        new = [i for i in found.get("items", []) if i["id"] not in seen]
        if not new:
            return
        seen.update(i["id"] for i in new)
        for stale in new:
            tags = stale.get("tags") or []
            at = results.parse_time(stale.get("start"))
            if (
                MADE_BY_BRIDGE in tags
                and f"band-{slug(band)}" in tags
                and (stale.get("properties") or {}).get("workflow") == workflow
                and stale["id"] not in kept
                and at
                and start <= at < end
            ):
                try:
                    yamcs("DELETE", f"/timeline/{INSTANCE}/items/{stale['id']}")
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                wrote("timeline_deleted")
        after = found.get("continuationToken") or ""
        if not after:
            return


_bands: dict[str, str] = {}


def ensure_band(name: str) -> None:
    """The mapping's band and the "Constellation forecast" view holding it, made once if missing.
    Never edits a band or view someone else made."""
    if not CREATE_VIEWS or name in _bands:
        return
    found = {
        b["name"]: b["id"] for b in yamcs("GET", f"/timeline/{INSTANCE}/bands").get("bands", [])
    }
    if name not in found:
        band = {"name": name, "type": "ITEM_BAND", "source": "rdb", "shared": True}
        found[name] = yamcs(
            "POST", f"/timeline/{INSTANCE}/bands", {**band, "tags": [f"band-{slug(name)}"]}
        )["id"]
    if "UTC" not in found:
        ruler = {
            "name": "UTC",
            "type": "TIME_RULER",
            "shared": True,
            "properties": {"timezone": "UTC"},
        }
        found["UTC"] = yamcs("POST", f"/timeline/{INSTANCE}/bands", ruler)["id"]
    _bands[name] = found[name]
    views = yamcs("GET", f"/timeline/{INSTANCE}/views").get("views", [])
    view = next((v for v in views if v["name"] == VIEW), None)
    if view is None:
        yamcs(
            "POST",
            f"/timeline/{INSTANCE}/views",
            {
                "name": VIEW,
                "description": "Predictions from Constellation, not scheduled contacts",
                "bands": [found["UTC"], found[name]],
            },
        )
    elif found[name] not in [b["id"] for b in view.get("bands", [])]:
        bands = [b["id"] for b in view.get("bands", [])] + [found[name]]
        yamcs("PUT", f"/timeline/{INSTANCE}/views/{view['id']}", {**view, "bands": bands})


def apply(
    item: dict[str, Any], mapping: dict[str, Any], tokens: dict[str, str], run: dict[str, Any]
) -> str | None:
    """Writes one item; returns the id of the timeline item it wrote, if any."""
    values = fields(item, tokens)
    state = results.action_state(item)
    # A held action is decided in Constellation; its event comes once it is approved. A rejected
    # one leaves the original pass, which the workflow's forecast already shows: no event, and
    # its timeline item (written while it was held) is re-tagged rather than left awaiting. An
    # action past its cancel deadline is closed: no event, approved or not.
    closed = results.past_deadline(item)
    if mapping["events"]["on"] and state not in ("pending", "rejected") and not closed:
        send_event(item, mapping["events"], values, run)
    if mapping["timeline"]["on"] and item.get("until"):
        ensure_band(mapping["timeline"]["band"])
        return timeline(item, mapping["timeline"], values, run)
    return None


_listed: list[bool] = []


def ensure_parameter_list() -> None:
    """A "Constellation forecast" parameter list over /ConstellationForecast, made once if missing."""
    if not CREATE_VIEWS or _listed:
        return
    lists = yamcs("GET", f"/parameter-lists/{INSTANCE}/lists").get("lists", [])
    if not any(item["name"] == VIEW for item in lists):
        yamcs(
            "POST",
            f"/parameter-lists/{INSTANCE}/lists",
            {
                "name": VIEW,
                "description": "Forecasts for the next pass and the next 24 hours; not measured telemetry",
                "patterns": [f"{SPACE}/*"],
            },
        )
    _listed.append(True)


def set_parameters(values: dict[str, Any]) -> None:
    def typed(v: Any) -> dict[str, Any]:
        if isinstance(v, float):
            return {"type": "FLOAT", "floatValue": v}
        if isinstance(v, int):
            return {"type": "UINT32", "uint32Value": v}
        return {"type": "STRING", "stringValue": str(v)}

    yamcs(
        "POST",
        f"/processors/{INSTANCE}/{PROCESSOR}/parameters:batchSet",
        {
            "request": [
                {"id": {"name": f"{SPACE}/{k}"}, "value": typed(v)} for k, v in values.items()
            ]
        },
    )



def priced_passes(found: list[dict[str, Any]], tokens: dict[str, str]) -> dict[str, Any]:
    """The run's forecast passes by id, each with its tokens: passes with no action of any kind
    (advice, approved, held or rejected) and no plan (series). A pass on a radio with no RF data
    has no expected GB; there is nothing to show for it."""
    out = {}
    for item in found:
        if (
            item["entity"]["type"] != "pass"
            or "series" in item
            or results.action_state(item) is not None
        ):
            continue
        values = fields(item, tokens)
        gb = [values.get(f"expected_gb.{q}") for q in results.QUANTILES]
        if all(isinstance(v, int | float) for v in gb):
            out[item["entity"]["id"]] = {"item": item, "values": values}
    return out


def summary(passes: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """The /ConstellationForecast parameters from the newest run's priced passes."""
    upcoming = sorted(
        (p for p in passes if datetime.fromisoformat(p["item"]["until"]) > now),
        key=lambda p: p["item"]["target_at"],
    )
    day = [
        p
        for p in upcoming
        if datetime.fromisoformat(p["item"]["target_at"]) < now + timedelta(hours=24)
    ]
    values: dict[str, Any] = {
        "passes_next_24h": len(day),
        "expected_gb_next_24h_p50": round(sum(p["values"]["expected_gb.p50"] for p in day), 3),
    }
    if upcoming:
        nxt, entity = upcoming[0]["values"], upcoming[0]["item"]["entity"]
        values |= {
            f"next_pass_expected_gb_{q}": float(nxt[f"expected_gb.{q}"]) for q in results.QUANTILES
        }
        values |= {
            "next_pass_aos": upcoming[0]["item"]["target_at"],
            "next_pass_satellite": entity["satellite_id"],
            "next_pass_station": entity["ground_station_id"],
        }
        if isinstance(nxt.get("max_elevation_deg"), int | float):
            values["next_pass_max_elevation"] = float(nxt["max_elevation_deg"])
    return values



_bucket_ready: list[bool] = []


def publish_widget(
    resource: dict[str, Any], batch: list[dict[str, Any]], widget: dict[str, Any]
) -> None:
    """The run's results and layout where the Constellation Forecast page reads them: one object
    per workflow, plus an index of workflows."""
    if not _bucket_ready:
        names = [b["name"] for b in yamcs("GET", "/storage/buckets").get("buckets", [])]
        if BUCKET not in names:
            yamcs("POST", "/storage/buckets", {"name": BUCKET})
        _bucket_ready.append(True)
    slug_ = resource.get("constellation.workflow") or "workflow"
    entry = {
        "workflow": slug_,
        "workflow_version": resource.get("constellation.workflow_version"),
        "kind": resource.get("constellation.template") or results.kind(batch),
        "run_id": resource.get("constellation.run_id"),
        "delivered_at": datetime.now(UTC).isoformat(),
        "blocks": widget["blocks"],
        "count": len(batch),
    }
    objects = f"/storage/buckets/{BUCKET}/objects"
    yamcs(
        "POST",
        f"{objects}/workflow-{slug_}.json",
        raw=json.dumps({**entry, "items": batch}).encode(),
        kind="application/json",
    )
    try:
        index = yamcs("GET", f"{objects}/index.json") or {}
    except urllib.error.HTTPError:
        index = {}
    workflows = {w["workflow"]: w for w in index.get("workflows", [])}
    workflows[slug_] = entry
    listed = {"workflows": sorted(workflows.values(), key=lambda w: w["workflow"])}
    yamcs("POST", f"{objects}/index.json", raw=json.dumps(listed).encode(), kind="application/json")
    wrote("widget_updates")


# --- telling Constellation --------------------------------------------------------------------


def grants(user: dict[str, Any]) -> dict[str, bool]:
    """What this bridge's Yamcs account may write, from GET /api/user."""
    if user.get("superuser"):
        return {"events": True, "timeline": True, "parameters": True, "buckets": True}
    system = set(user.get("systemPrivileges") or [])
    objects = {p["type"]: p.get("objects", []) for p in user.get("objectPrivileges") or []}
    return {
        "events": "WriteEvents" in system,
        "timeline": "ControlTimeline" in system,
        "parameters": "WriteParameter" in objects,
        "buckets": "ManageAnyBucket" in system or BUCKET in objects.get("ManageBucket", []),
    }


def check_in() -> None:
    """Tell Constellation what this bridge found on its side; raises only Refused."""
    try:
        server = yamcs("GET", "")
        plugins = {p["name"]: p.get("version") for p in server.get("plugins", [])}
        constellation(
            "POST",
            "/predictions/feed/checkin",
            {
                "consumer": CONSUMER,
                "system": "yamcs",
                "target": f"Yamcs {INSTANCE} at {YAMCS}",
                "system_version": server.get("yamcsVersion"),
                "bridge_version": VERSION,
                "mapping_versions": MAPPING_VERSIONS,
                "can": grants(yamcs("GET", "/user")),
                "widget": {
                    "installed": WIDGET_PLUGIN in plugins,
                    "version": plugins.get(WIDGET_PLUGIN),
                },
            },
        )
    except Refused:
        raise
    except Exception as exc:
        print(f"bridge: could not check in ({exc})", file=sys.stderr, flush=True)


def report(cursor: str) -> None:
    """Tell Constellation what this entry became in Yamcs; a failed report never stops delivery,
    a refusal (Refused) does."""
    try:
        results.report(constellation, CONSUMER, cursor, dict(written), f"Yamcs {INSTANCE} at {YAMCS}")
    except Refused:
        raise
    except Exception as exc:
        print(f"bridge: could not report delivery ({exc})", file=sys.stderr, flush=True)


# --- the loop ---------------------------------------------------------------------------------


def load_cursor() -> str:
    """The last cursor applied, from CURSOR_FILE; empty when none was ever saved."""
    try:
        with open(CURSOR_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def load_newest() -> dict[str, str]:
    try:
        with open(NEWEST_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def keep_newest(entry: dict[str, Any], cursor: str) -> None:
    newest = load_newest() | dict.fromkeys(workflows_of(entry), cursor)
    with open(f"{NEWEST_FILE}.tmp", "w") as f:
        json.dump(newest, f)
    os.replace(f"{NEWEST_FILE}.tmp", NEWEST_FILE)


def save_cursor(cursor: str) -> None:
    """In CURSOR_FILE, and in Yamcs, where a cursor that does not match says Yamcs restarted."""
    with open(f"{CURSOR_FILE}.tmp", "w") as f:
        f.write(cursor)
    os.replace(f"{CURSOR_FILE}.tmp", CURSOR_FILE)
    set_parameters({"feed_cursor": cursor})


def saved_cursor() -> str:
    """The last cursor applied as Yamcs holds it, once Yamcs answers; empty when Yamcs has none
    (never saved, or Yamcs restarted since)."""
    while True:
        try:
            value = yamcs(
                "GET", f"/processors/{INSTANCE}/{PROCESSOR}/parameters{SPACE}/feed_cursor"
            )
            return str(value.get("engValue", {}).get("stringValue", ""))
        except urllib.error.HTTPError:
            return ""
        except urllib.error.URLError as exc:
            log(f"waiting for Yamcs ({exc.reason})")
            time.sleep(POLL_S)



def deliver(entry: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    """One feed entry into Yamcs: each of this connection's resources by the mapping its output
    chose. Returns the newest priced passes, for the parameters. An output this bridge cannot
    apply is skipped, logged and counted as refused, a malformed item as malformed and an item
    Yamcs answers with a 4xx as rejected, so none holds up the runs after it. A Yamcs 5xx (after
    its retries) or outage is raised: the run is not acked, and the next poll delivers it again."""
    written.clear()
    for resource, found in results.unpack(entry["otlp"], CONSUMER, log):
        output = json.loads(resource.get("constellation.output") or "{}")
        system = resource.get("constellation.system")
        if system != "yamcs":
            why = f"it is a {system} output, not a Yamcs one"
        elif output.get("mapping_version") not in MAPPING_VERSIONS:
            why = f"its mapping version {output.get('mapping_version')} needs a newer bridge"
        else:
            why = ""
        if why:
            log(f"refused an output of run {resource.get('constellation.run_id')}: {why}")
            wrote("refused")
            continue
        mapping, tokens = output["mapping"], output["tokens"]
        run = {
            "workflow": resource.get("constellation.workflow") or "workflow",
            "workflow_version": resource.get("constellation.workflow_version"),
            "run_id": resource.get("constellation.run_id"),
            "made_at": results.made_at(entry["otlp"]),
        }
        good, kept, skipped = [], set(), False
        for n, item in enumerate(found):
            try:
                results.check(item)
                if made := apply(item, mapping, tokens, run):
                    kept.add(made)
            except results.MALFORMED as exc:
                log(f"skipped item {n} of run {run['run_id']}: it is malformed ({exc!r})")
                wrote("malformed")
                skipped = True
                continue
            except urllib.error.HTTPError as exc:
                if exc.code == 404 or exc.code >= 500:
                    raise
                answer = exc.read()[:300].decode(errors="replace")
                log(
                    f"skipped item {n} of run {run['run_id']}: Yamcs refused it"
                    f" (HTTP {exc.code} {answer})"
                )
                wrote("rejected")
                skipped = True
            good.append(item)
        # The run is the workflow's whole answer: what it no longer carries is withdrawn. Not
        # when an item was skipped, as its old timeline item would be taken for withdrawn.
        if mapping["timeline"]["on"] and not skipped:
            window = (
                resource.get("constellation.window.from"),
                resource.get("constellation.window.to"),
            )
            prune_timeline(run["workflow"], mapping["timeline"]["band"], kept, window)
        try:
            # A pass-yield run is the whole forecast for its window: the parameters are that run's.
            passes = priced_passes(good, tokens)
            if passes and mapping["parameters"]["on"]:
                latest = passes
                ensure_parameter_list()
                values = summary(list(latest.values()), datetime.now(UTC))
                set_parameters(values)
                wrote("parameters", len(values))
            widget = output.get("widget")
            if widget and widget["on"]:
                publish_widget(resource, good, widget)
        except results.MALFORMED as exc:
            log(
                f"skipped the parameters and widget of run {run['run_id']}: an item is"
                f" malformed ({exc!r})"
            )
            wrote("malformed")
    return latest


def workflows_of(entry: dict[str, Any]) -> set[str]:
    return {
        r.get("constellation.workflow") or "workflow"
        for r, _ in results.unpack(entry["otlp"], CONSUMER, lambda _: None)
    }


def republish(upto: str, latest: dict[str, Any]) -> dict[str, Any]:
    """Yamcs lost what this bridge made (a restart without persistent storage): makes it again
    and applies the newest run of every workflow up to cursor `upto` once more, so the page and
    the parameters show every workflow, not only the one that ran last."""
    _bands.clear()
    _bucket_ready.clear()
    _listed.clear()
    newest = {w: c for w, c in load_newest().items() if c <= upto}
    log(f"Yamcs lost what this bridge made; applying the newest run of {len(newest)} workflows again")
    for cursor in sorted(set(newest.values())):
        try:
            run = whole_run(cursor)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # Expired (kept 7 days): that workflow fills in again with its next run.
            log(f"run {cursor} is no longer in the feed; its workflow fills in with its next run")
            continue
        latest = deliver(run, latest)
    written.clear()
    set_parameters({"feed_cursor": upto})
    return latest


def catch_up(cursor: str) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """The runs after `cursor`, each as (its cursor, the run to apply), read 20 at a time and
    each read whole only when it is applied. A run whose every workflow has a newer run among
    those 20 is superseded: it comes with None, so the cursor passes it but nothing is written,
    and downtime does not replay old approvals. The newest run of each workflow is applied."""
    after = cursor
    while True:
        page = runs_after(after)
        if after == cursor and (lost := results.expired(page, cursor)):
            print(f"bridge: {lost}", file=sys.stderr, flush=True)
        if not page["entries"]:
            return
        workflows = [workflows_of(entry) for entry in page["entries"]]
        for n, entry in enumerate(page["entries"]):
            if not workflows[n]:
                yield entry["cursor"], entry
            elif workflows[n] <= set().union(*workflows[n + 1 :]):
                yield entry["cursor"], None
            else:
                yield entry["cursor"], whole_run(entry["cursor"])
        after = page["next"]


def backoff(exc: Exception, failed: int) -> float:
    """How long to wait after `failed` polls in a row went wrong: a 429's Retry-After, at most
    60 s; otherwise POLL_S, doubling up to 5 minutes."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        try:
            return max(1.0, min(float(exc.headers.get("Retry-After")), 60.0))
        except (TypeError, ValueError):
            pass
    return min(POLL_S * 2 ** (failed - 1), 300)


def writable() -> None:
    """Exits 2 with why unless the bridge can keep its state: carrying on would forget what it
    announced as approved and announce it again after a restart."""
    for folder in {STATE_DIR, os.path.dirname(ACTED_FILE)}:
        probe = os.path.join(folder, ".yamcs-bridge-probe")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(probe, "w") as f:
                f.write(VERSION)
                f.flush()
                os.fsync(f.fileno())
            os.remove(probe)
        except OSError as exc:
            print(
                f"bridge: cannot keep its state in {folder} ({exc.strerror}): mount a writable"
                " volume there (compose.yaml does) or set STATE_DIR to a writable directory",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(2)


def main() -> None:
    if not API_KEY:
        print("bridge: CONSTELLATION_API_KEY is not set", file=sys.stderr, flush=True)
        sys.exit(2)
    if why := results.key_refusal(API):
        print(f"bridge: {why}", file=sys.stderr, flush=True)
        sys.exit(2)
    writable()
    in_yamcs, saved = saved_cursor(), load_cursor()
    cursor: str | None = saved or in_yamcs or None
    lost = bool(saved) and in_yamcs != saved
    if cursor and not saved:
        # Before CURSOR_FILE the cursor was only in Yamcs: an upgraded bridge moves it here.
        save_cursor(cursor)
    latest: dict[str, Any] = {}
    log(f"{API} -> {YAMCS}/{INSTANCE} as {CONSUMER}, from cursor {cursor!r}, state in {STATE_DIR}")
    checked, failed, said = 0.0, 0, ""
    while True:
        try:
            if time.time() - checked > results.CHECKIN_S:
                check_in()
                checked = time.time()
            if cursor is None:
                cursor = results.start_cursor(constellation, CONSUMER)
                log(f"first start: reading the feed after {cursor!r}")
            if lost:
                latest = republish(cursor, latest)
                lost = False
            caught_up = True
            for reached, entry in catch_up(cursor):
                caught_up = False
                if entry is None:
                    log(f"skipped run {reached}: a newer run of its workflow follows")
                else:
                    try:
                        latest = deliver(entry, latest)
                    except urllib.error.HTTPError as exc:
                        if exc.code != 404:
                            raise
                        latest = deliver(entry, republish(cursor, latest))
                    keep_newest(entry, reached)
                cursor = reached
                save_cursor(cursor)
                if written:
                    report(cursor)
                    log(f"applied {dict(written)}, cursor {cursor}")
                written.clear()
            # Yamcs restarted without storage since the last run: make again what it lost now,
            # not only when the next run arrives.
            if caught_up and load_cursor() == cursor and saved_cursor() != cursor:
                latest = republish(cursor, latest)
            if caught_up and latest:
                set_parameters(summary(list(latest.values()), datetime.now(UTC)))
            failed, said = 0, ""
        except Refused as exc:
            # Said once; a restart loop or a poll every few seconds would only re-hit the refusal.
            if str(exc) != said:
                print(
                    f"bridge: {exc}. Asking again every {exc.wait / 60:.0f} minutes until then",
                    file=sys.stderr,
                    flush=True,
                )
                said = str(exc)
            time.sleep(exc.wait)
            continue
        except Exception as exc:  # a bad poll is logged and retried, never fatal
            print(f"bridge: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            failed += 1
            time.sleep(backoff(exc, failed))
            continue
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
