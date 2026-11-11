"""Workflow results, as every connector here reads them: pulling a connection's runs, joining
a paged run whole, reading its items, and reporting what was applied.

`call(method, path, body=None)` is the bridge's own Constellation API call (its base URL, its key,
its timeouts); `log(message)` its own log line. The COSMOS plugin's gem carries a copy of this
module (as constellation_space/results.py), which its Rakefile makes at build time. Standard library only,
and it imports nothing else from this package, so that copy stands alone.
"""

import ipaddress
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

# The item shape these bridges read; results in another are skipped and logged, never guessed at.
SCHEMA = "constellation.item/v2"
# The most records the API sends in one page of a run; it pages a run with more (a few MB each).
PAGE_RECORDS = 400
PLATFORM = "https://platform.constellation.space"


def site(api_url: str | None = None) -> str:
    """The Constellation site that serves `api_url` (default CONSTELLATION_API_URL), where its
    keys and billing pages are: api.<domain> is platform.<domain>, <env>.api.<domain> is
    <env>-platform.<domain>, and a personal sandbox's s-<id>.api.<domain> is
    platform.s-<id>.<domain>. Any other API is told the public site."""
    host = urllib.parse.urlparse(api_url or os.environ.get("CONSTELLATION_API_URL", "")).hostname
    if host and host.startswith("api."):
        return f"https://platform.{host[4:]}"
    if host and ".api." in host:
        env, domain = host.split(".api.", 1)
        if env.startswith("s-"):
            return f"https://platform.{env}.{domain}"
        return f"https://{env}-platform.{domain}"
    return PLATFORM


def loopback(url: str) -> bool:
    """Whether `url` names this machine: localhost or a loopback address."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin(url: str) -> str:
    """A URL's scheme and host, lowercase, its port only when it is not the scheme's default, and
    any name for this machine (localhost, 127.0.0.1, ::1) as localhost: two URLs with one origin
    are the same API."""
    u = urllib.parse.urlparse(url.strip())
    scheme, host = u.scheme.lower(), "localhost" if loopback(url.strip()) else u.hostname or ""
    host = f"[{host}]" if ":" in host else host
    port = u.port if u.port not in (None, {"https": 443, "http": 80}.get(scheme)) else None
    return f"{scheme}://{host}" + (f":{port}" if port else "")


def key_safe(url: str) -> bool:
    """Whether an API key may go to `url`: over https, or plain http to this machine."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    return scheme == "https" or (scheme == "http" and loopback(url))


def key_refusal(url: str) -> str:
    """Why an API key may not go to `url`, or "" when it may (key_safe)."""
    if key_safe(url):
        return ""
    return f"not sending the API key to {url}: it is not https (plain http only to localhost)"


def http(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
    timeout: float = 60,
) -> Any:
    """One HTTP call: `body` sent as JSON (or `raw` as is), the answer's JSON back, or None for an
    answer that is not JSON. An HTTP error is raised as urllib's HTTPError."""
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None and raw is None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    return json.loads(payload) if payload and payload[:1] in b"{[" else None


def action_state(item: dict[str, Any]) -> str | None:
    """What an item's action is to a bridge. "advice": suggested, never an instruction.
    "approved": a person approved it in Constellation, an instruction to carry out. "pending":
    held for approval; the action itself is not sent, only action_status. "rejected": decided
    against; the original stands. None: the item has no action. Any status other than approved
    or rejected counts as held, so an item a bridge cannot read is never taken for a plain
    forecast."""
    status = item.get("action_status")
    if status is not None and status != "approved":
        return "rejected" if status == "rejected" else "pending"
    if not item.get("action"):
        return None if status is None else "pending"
    return "approved" if status == "approved" else "advice"


def action_key(item: dict[str, Any]) -> str:
    """Names the decision to carry out an action: its decision_id, which a standing approval keeps
    in every run that carries it and a new approval of the same action changes. A result without
    one is named by its content, as Constellation keys an action: the same verb on the same entity
    to the same target."""
    if decision := item.get("decision_id"):
        return str(decision)
    action = item.get("action") or {}
    return f"{item['entity']['id']}|{action.get('verb') or ''}|{action.get('to') or ''}"


# How often a bridge checks in again: the platform reads a connection that stopped checking in
# as stopped. A check-in that failed is tried again sooner.
CHECKIN_S = 300


# How long an acted key is kept once its action is over: past it, no run sends the action again.
KEEP_ACTED = timedelta(days=7)


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """An ISO 8601 time, UTC when it names no zone; None when it is not one."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def acted_until(item: dict[str, Any]) -> str:
    """When an acted key may be forgotten: the latest of the item's time, its end (a pass's end),
    its action's cancel deadline and now, plus KEEP_ACTED, so a key is never dropped as soon as it
    is written. "" (kept for good) when none of the item's is a time."""
    action = item.get("action") or {}
    times = [
        parse_time(v) for v in (item.get("target_at"), item.get("until"), action.get("deadline"))
    ]
    known = [t for t in times if t]
    if not known:
        return ""
    return (max(*known, now()) + KEEP_ACTED).astimezone(timezone.utc).isoformat(timespec="seconds")


def first_acted(items: list[dict[str, Any]], acted: dict[str, str]) -> int:
    """How many of these items are approved actions still open (before their cancel deadline)
    carried out for the first time. A bridge that enforces or shows approvals on every run, rather
    than acting once, counts each decision (action_key) once: `acted` maps each counted to when
    it may be forgotten (acted_until), kept in the bridge's state, and loses keys long over."""
    _drop_over(acted)
    first = 0
    for item in items:
        key = action_key(item)
        if action_state(item) == "approved" and not past_deadline(item) and key not in acted:
            acted[key] = acted_until(item)
            first += 1
    return first


def _drop_over(keys: dict[str, str]) -> bool:
    """Drops the keys whose time to be forgotten has passed; whether any was."""
    cutoff = now()
    over = [k for k, until in keys.items() if (t := parse_time(until)) and t < cutoff]
    for key in over:
        del keys[key]
    return bool(over)


def past_deadline(item: dict[str, Any]) -> bool:
    """Whether the item's action is past its cancel deadline: closed, never to be carried out."""
    deadline = parse_time((item.get("action") or {}).get("deadline"))
    return deadline is not None and deadline <= now()


class Acted:
    """The approved actions already carried out, in the file `path`: one line per action_key, a
    tab, and when it may be forgotten (acted_until). Constellation sends an approved action again
    (the run is republished when it is approved, and later runs carry it too), so a bridge checks
    here before acting, and neither a later run nor a restart acts twice. Keys whose action is long
    over are dropped whenever the file is written."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.keys: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f.read().splitlines():
                    key, _, until = line.partition("\t")
                    if key:
                        self.keys[key] = until
        except FileNotFoundError:
            pass
        if _drop_over(self.keys):
            self._save()

    def __contains__(self, item: dict[str, Any]) -> bool:
        return action_key(item) in self.keys

    def add(self, item: dict[str, Any]) -> None:
        self.keys[action_key(item)] = acted_until(item)
        _drop_over(self.keys)
        self._save()

    def _save(self) -> None:
        with open(f"{self.path}.tmp", "w") as f:
            f.writelines(f"{key}\t{until}\n" for key, until in self.keys.items())
        os.replace(f"{self.path}.tmp", self.path)


# What reading a malformed item raises: a bridge skips, logs and counts the item, never retries it.
MALFORMED = (KeyError, TypeError, ValueError, AttributeError, IndexError)


def check(item: Any) -> None:
    """Raises ValueError unless the item has what every bridge reads: an entity with a string id
    and type, and times (target_at, until when it has one, and its action's deadline)."""
    entity = item.get("entity") if isinstance(item, dict) else None
    if not isinstance(entity, dict) or not all(
        isinstance(entity.get(k), str) for k in ("id", "type")
    ):
        raise ValueError("no entity with an id and a type")
    datetime.fromisoformat(item["target_at"])
    if item.get("until") is not None:
        datetime.fromisoformat(item["until"])
    action = item.get("action")
    if action is not None and not isinstance(action, dict):
        raise ValueError("its action is not an object")
    if (action or {}).get("deadline") is not None:
        datetime.fromisoformat(action["deadline"])


def _detail(body: bytes | str) -> dict[str, Any]:
    """An API error's detail: FastAPI's {"detail": {...}} or a bare {"error": ...}."""
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    detail = answer.get("detail", answer) if isinstance(answer, dict) else None
    return detail if isinstance(detail, dict) else {}


def well_formed(
    items: list[dict[str, Any]], run_id: Any, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """The items a bridge can read (check()); each other one is logged and skipped, so one
    malformed item never holds up its run and the runs after it."""
    good = []
    for n, item in enumerate(items):
        try:
            check(item)
            good.append(item)
        except MALFORMED as exc:
            log(f"skipped item {n} of run {run_id}: it is malformed ({exc!r})")
    return good


def refusal(
    status: int, body: bytes | str, needs: str = "predictions:read", api_url: str | None = None
) -> str | None:
    """Why Constellation refused this bridge, and the fix, for a 401 or 403; None for any other
    status. Neither clears by asking again, so a bridge stops polling on one. `needs` is the
    scopes the bridge's key needs; the fix links the site of `api_url` (site())."""
    if status not in (401, 403):
        return None
    detail = _detail(body)
    fix = f"make a key with {needs} on {site(api_url)}/keys and set it as CONSTELLATION_API_KEY"
    error = detail.get("error")
    if status == 401:
        return (
            "Constellation does not accept CONSTELLATION_API_KEY (HTTP 401; wrong, revoked or"
            f" expired): {fix}"
        )
    if error == "plan_required":
        return (
            f"this account's plan does not include {detail.get('feature')} (HTTP 403"
            f" plan_required); the {detail.get('plan')} plan does. Upgrade on {site(api_url)}/billing"
        )
    if error == "insufficient_scope":
        return f"CONSTELLATION_API_KEY lacks {detail.get('required')} (HTTP 403): {fix}"
    if error == "live_key_required":
        return f"CONSTELLATION_API_KEY is a test key; results need a live one (HTTP 403): {fix}"
    return f"Constellation refused CONSTELLATION_API_KEY (HTTP 403 {error or 'forbidden'}): {fix}"


def held_off(
    status: int, body: bytes | str, api_url: str | None = None
) -> tuple[str, float] | None:
    """Why Constellation asks this bridge to wait, and how many seconds, for a 402 or 429; None
    for any other status. Asking sooner never helps: a lockout also blocks a corrected key."""
    if status not in (402, 429):
        return None
    detail = _detail(body)
    error = detail.get("error") or ("payment_required" if status == 402 else "too_many_requests")
    retry = detail.get("retry_after_seconds")
    wait = float(retry) if isinstance(retry, (int, float)) and retry > 0 else 0.0
    if status == 402:
        return (
            f"this account's plan does not cover it (HTTP 402 {error}). Upgrade on"
            f" {site(api_url)}/billing",
            max(wait, 3600),
        )
    if error == "auth_lockout":
        return (
            "Constellation locked out this address after repeated refused keys (HTTP 429"
            " auth_lockout); until it lifts it refuses even a corrected key",
            max(wait, 60),
        )
    return f"Constellation asks this bridge to slow down (HTTP 429 {error})", max(wait, 60)


def plain(value: dict[str, Any]) -> Any:
    """An OTLP AnyValue back to plain JSON."""
    if "kvlistValue" in value:
        return {kv["key"]: plain(kv["value"]) for kv in value["kvlistValue"].get("values", [])}
    if "arrayValue" in value:
        return [plain(v) for v in value["arrayValue"].get("values", [])]
    if "intValue" in value:
        return int(value["intValue"])
    for key in ("doubleValue", "boolValue", "stringValue"):
        if key in value:
            return value[key]
    return None


def newer_shapes(records: list[dict[str, Any]]) -> set[Any]:
    """The item shapes among these records that this bridge does not read."""
    return {
        next(
            (
                plain(kv["value"])
                for kv in r.get("attributes", [])
                if kv["key"] == "constellation.schema"
            ),
            None,
        )
        for r in records
    } - {SCHEMA}


def unpack(
    otlp: dict[str, Any], consumer: str, log: Callable[[str], None] = print
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """A results entry's parts for this connection: each resourceLogs (one per feed output of the
    run) whose constellation.connection is `consumer`, as its resource attributes and its items.
    Parts for other connections are left alone; a part in an item shape this bridge does not read
    is skipped and logged."""
    parts = []
    for logs in otlp.get("resourceLogs", []):
        resource = {
            kv["key"]: plain(kv["value"]) for kv in logs.get("resource", {}).get("attributes", [])
        }
        if resource.get("constellation.connection") != consumer:
            continue
        records = [r for scope in logs.get("scopeLogs", []) for r in scope.get("logRecords", [])]
        if newer := newer_shapes(records):
            log(
                f"skipped run {resource.get('constellation.run_id')}: its items are "
                f"{', '.join(map(str, newer))} and this bridge reads {SCHEMA}"
            )
            continue
        parts.append((resource, [plain(r["body"]) for r in records]))
    return parts


def expired(page: dict[str, Any], cursor: str) -> str | None:
    """Why runs were lost when the feed no longer holds `cursor` (it sorts before the page's
    oldest entry: runs after it expired unread); None when it still does."""
    oldest = page.get("oldest")
    if not cursor or not oldest or cursor >= oldest:
        return None
    return (
        f"the feed no longer holds cursor {cursor}: any runs after it and before the oldest it"
        f" holds, {oldest}, expired unread; reading on from there"
    )


def results_page(call: Callable[..., Any], consumer: str, after: str) -> dict[str, Any]:
    """The next run after cursor `after`, its first page only (whole() joins the rest): one run
    per page, as a run's results with their ledgers can be several MB."""
    query = {"after": after, "limit": 1, "consumer": consumer, "page_records": PAGE_RECORDS}
    return call("GET", f"/predictions/feed?{urllib.parse.urlencode(query)}")


def rest_of_run(call: Callable[..., Any], consumer: str, cursor: str, page: int) -> dict[str, Any]:
    """Page `page` of the run at `cursor`."""
    query = {"cursor": cursor, "page": page, "page_records": PAGE_RECORDS, "consumer": consumer}
    return call("GET", f"/predictions/feed?{urllib.parse.urlencode(query)}")


def whole(entry: dict[str, Any], fetch: Callable[[str, int], dict[str, Any]]) -> dict[str, Any]:
    """`entry` with every page of its run joined in: each page's records added to their own
    resource, in order. `fetch(cursor, page)` reads one page. A run is applied whole or not at
    all, never a page of it."""
    entry = {
        **entry,
        "otlp": {**entry["otlp"], "resourceLogs": list(entry["otlp"]["resourceLogs"])},
    }
    while entry.get("next_page"):
        more = fetch(entry["cursor"], entry["next_page"])
        by_resource = {
            json.dumps(r["resource"], sort_keys=True): r for r in entry["otlp"]["resourceLogs"]
        }
        for logs in more["otlp"]["resourceLogs"]:
            key = json.dumps(logs["resource"], sort_keys=True)
            if key in by_resource:
                scope = by_resource[key]["scopeLogs"][0]
                scope["logRecords"] = scope["logRecords"] + logs["scopeLogs"][0]["logRecords"]
            else:
                entry["otlp"]["resourceLogs"].append(logs)
                by_resource[key] = logs
        entry["next_page"] = more.get("next_page")
    entry.pop("next_page", None)
    return entry


def start_cursor(call: Callable[..., Any], consumer: str) -> str:
    """Where a first start reads from: just before the newest run with something for this
    connection, so it applies that run and none of the older ones. The API answers it without
    reading a run (from=latest, no entries)."""
    query = {"from": "latest", "after": "", "limit": 20, "consumer": consumer, "page_records": 1}
    page = call("GET", f"/predictions/feed?{urllib.parse.urlencode(query)}")
    if page["entries"]:
        raise RuntimeError(
            "the API ignored GET /predictions/feed?from=latest, so a first start cannot find the"
            " newest run: this bridge needs an API serving contract 1.5.0 or later"
        )
    return page["next"]


def report(
    call: Callable[..., Any], consumer: str, cursor: str, applied: dict[str, int], target: str
) -> None:
    """Tells Constellation what the run at `cursor` became in the system, shown in Constellation;
    raises when Constellation does not take it."""
    call(
        "POST",
        "/predictions/feed/deliveries",
        {
            "consumer": consumer,
            "run_id": cursor.split("#", 1)[1],
            "cursor": cursor,
            "applied": applied,
            "target": target,
        },
    )


def kind(items: list[dict[str, Any]]) -> str:
    """What a run's items are, by what they carry, named as the template that makes them: the
    Forecast page picks its view by it. Read only when the run does not name its template (the
    contract does not require it)."""
    explained = {k for i in items for k in i.get("explain") or {}}
    if any(i["entity"]["type"] == "antenna" for i in items):
        return "antenna-capacity"
    if any((i.get("action") or {}).get("verb") == "swap" for i in items):
        return "rain-fade-swap"
    if any(i.get("series") for i in items):
        return "modcod-plan"
    for column, name in (("ledger", "audit-ledger"), ("rfi_db", "rfi-watch")):
        if column in explained:
            return name
    return "pass-yield"


def made_at(otlp: dict[str, Any]) -> str | None:
    """When a run was made: the API stamps each of its records with the time it produced them."""
    times = [
        int(r["timeUnixNano"])
        for logs in otlp.get("resourceLogs", [])
        for scope in logs.get("scopeLogs", [])
        for r in scope.get("logRecords", [])
        if r.get("timeUnixNano")
    ]
    if not times:
        return None
    return datetime.fromtimestamp(min(times) / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


def max_elevation(item: dict[str, Any]) -> float | None:
    """A pass's max elevation: the ledger entry the orbit wrote, under whichever node wrote it."""
    for entry in (item.get("explain") or {}).values():
        if isinstance(entry, dict) and entry.get("source") == "orbit" and entry.get("unit") == "deg":
            return entry.get("value")
    return None


QUANTILES = ("p10", "p50", "p90")


def expected_gb(item: dict[str, Any]) -> dict[str, Any] | None:
    """A pass's expected GB, {p10, p50, p90}, every one a number: the ledger's expected_gb, else
    the item's value (the output column), as a copied workflow may have renamed the ledger node.
    None when neither has all three."""
    ledger = ((item.get("explain") or {}).get("expected_gb") or {}).get("value")
    for gb in (ledger, item.get("value")):
        if isinstance(gb, dict) and all(
            isinstance(gb.get(q), int | float) and not isinstance(gb.get(q), bool)
            for q in QUANTILES
        ):
            return gb
    return None
