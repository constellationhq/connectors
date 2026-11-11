"""The Constellation API's side of the feed, for tests: an item, items as the OTLP feed encodes
them, a feed output with its defaults filled in as a run publishes it, and the feed routes served
in process (ResultsApi).

A copy of the API's item and feed output encoding.
contract.json holds the API's tokens table and default mappings, dumped from the API at the item
schema and mapping version it names; regenerate it from the API when either changes.
"""

import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from constellation_space.client import Client

ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = json.loads((Path(__file__).parent / "contract.json").read_text())
SCHEMA_VERSION: str = _CONTRACT["schema_version"]
MAPPING_VERSION: int = _CONTRACT["mapping_version"]
TOKENS: dict[str, str] = _CONTRACT["tokens"]
YAMCS_DEFAULTS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    t: (d["mapping"], d["widget"]) for t, d in _CONTRACT["yamcs_defaults"].items()
}
LATTICE_DEFAULT: dict[str, Any] = _CONTRACT["lattice_default"]

_FACETS = ("series", "severity", "action")
_OPTIONAL = ("until", "value", "explain", *_FACETS)
_SEVERITY = {"info": 9, "warning": 13, "critical": 17}


def item(
    entity: dict[str, Any],
    target_at: str,
    value: dict[str, Any],
    *,
    until: str | None = None,
    explain: dict[str, Any] | None = None,
    workflow_version: int | None = None,
    **facets: Any,
) -> dict[str, Any]:
    """One item; a facet or optional key left None is omitted."""
    optional = {
        "until": until,
        "value": value,
        "explain": explain,
        "workflow_version": workflow_version,
        **facets,
    }
    return {
        "entity": entity,
        "target_at": target_at,
        **{k: v for k, v in optional.items() if v is not None},
    }


def output(
    connection: str = "yamcs-bridge",
    mapping: dict[str, Any] | None = None,
    widget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A feed output as the bridge receives it beside the items. No mapping is the Yamcs
    pass-yield default; a Lattice mapping gets the Lattice defaults for what it leaves out; a
    COSMOS one has no settings but the widget."""
    if mapping is not None and mapping.get("system") == "lattice":
        return {
            "connection": connection,
            "system": "lattice",
            "mapping_version": MAPPING_VERSION,
            "mapping": {**LATTICE_DEFAULT, **mapping},
            "tokens": TOKENS,
        }
    default_mapping, default_widget = YAMCS_DEFAULTS["pass-yield"]
    if mapping is not None and mapping.get("system") == "cosmos":
        return {
            "connection": connection,
            "system": "cosmos",
            "mapping_version": MAPPING_VERSION,
            "mapping": {"system": "cosmos"},
            "widget": widget or default_widget,
            "tokens": TOKENS,
        }
    return {
        "connection": connection,
        "system": "yamcs",
        "mapping_version": MAPPING_VERSION,
        "mapping": mapping or default_mapping,
        "widget": widget or default_widget,
        "tokens": TOKENS,
    }


def _any(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": _attributes(value)}}
    if isinstance(value, list | tuple):
        return {"arrayValue": {"values": [_any(v) for v in value]}}
    return {"stringValue": "" if value is None else str(value)}


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _any(v)} for k, v in values.items() if v is not None]


def _nanos(at: datetime) -> str:
    return str(int(at.timestamp() * 1_000_000_000))


def to_otlp(
    tenant_key: str,
    items: list[dict[str, Any]],
    observed: datetime,
    run: dict[str, Any],
    feed_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Items as one OTLP ExportLogsServiceRequest, for the run `run` names, on `feed_output`."""
    resource = {
        "constellation.tenant": tenant_key,
        "constellation.workflow": run["workflow"],
        "constellation.workflow_version": items[0].get("workflow_version") if items else None,
        "constellation.template": run.get("template"),
        "constellation.run_id": run["run_id"],
        "constellation.window.from": run.get("window_from"),
        "constellation.window.to": run.get("window_to"),
        "constellation.connection": feed_output["connection"] if feed_output else None,
        "constellation.system": feed_output["system"] if feed_output else None,
        "constellation.output": json.dumps(feed_output) if feed_output else None,
    }
    records = [
        {
            "timeUnixNano": _nanos(observed),
            "observedTimeUnixNano": _nanos(observed),
            "severityNumber": _SEVERITY[item.get("severity", "info")],
            "severityText": item.get("severity", "info").upper(),
            "body": _any(item),
            "attributes": _attributes(
                {
                    "constellation.schema": SCHEMA_VERSION,
                    "constellation.entity.type": item["entity"]["type"],
                    "constellation.entity.id": item["entity"]["id"],
                    "constellation.time.target": item["target_at"],
                    "constellation.time.until": item.get("until"),
                    "constellation.facets": [f for f in _OPTIONAL if f in item],
                }
            ),
        }
        for item in items
    ]
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attributes(resource)},
                "scopeLogs": [
                    {"scope": {"name": "constellation.workflows"}, "logRecords": records}
                ],
            }
        ]
    }


def tagged(otlp: dict[str, Any], system: str) -> dict[str, Any]:
    """`otlp` with every resource's constellation.system set to `system`, whatever its output
    says: as an API stamps it, and as an older one tagged a mapping-less output (yamcs)."""
    tag = {"key": "constellation.system", "value": {"stringValue": system}}
    return {
        **otlp,
        "resourceLogs": [
            {
                **logs,
                "resource": {
                    "attributes": [
                        a for a in logs["resource"]["attributes"] if a["key"] != tag["key"]
                    ]
                    + [tag]
                },
            }
            for logs in otlp["resourceLogs"]
        ],
    }


def _feed_query() -> dict[str, dict[str, Any]]:
    """GET /predictions/feed's query parameters, by name, from the OpenAPI contract.lock pins."""
    spec = json.loads((ROOT / "contract" / "openapi.json").read_text())
    params = spec["paths"]["/predictions/feed"]["get"]["parameters"]
    return {
        p["name"]: next((s for s in p["schema"].get("anyOf", [p["schema"]]) if s["type"] != "null"))
        for p in params
        if p["in"] == "query"
    }


FEED_QUERY = _feed_query()


class Unprocessable(ValueError):
    """What the API answers 422 for: a query its OpenAPI does not allow."""


def check_feed_query(query: dict[str, str]) -> None:
    """Raises Unprocessable for a feed query the API would refuse, as FastAPI validates it."""
    for name, value in query.items():
        schema = FEED_QUERY.get(name)
        if schema is None:
            continue
        if schema["type"] == "integer":
            try:
                number = int(value)
            except ValueError:
                raise Unprocessable(f"{name}={value!r} is not an integer") from None
            if number < schema.get("minimum", number) or number > schema.get("maximum", number):
                raise Unprocessable(
                    f"{name}={number} is outside {schema.get('minimum')}..{schema.get('maximum')}"
                )
        elif "pattern" in schema and not re.search(schema["pattern"], value):
            raise Unprocessable(f"{name}={value!r} does not match {schema['pattern']}")
        elif not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise Unprocessable(f"{name}={value!r} has the wrong length")


def start(entries: list[dict[str, Any]]) -> str:
    """What from=latest answers: just before the newest entry with resources, else the newest."""
    for n in range(len(entries) - 1, -1, -1):
        if entries[n]["otlp"]["resourceLogs"]:
            return entries[n - 1]["cursor"] if n else ""
    return entries[-1]["cursor"] if entries else ""


class ResultsApi(Client):
    """The feed routes over `entries` ({cursor, otlp}), as a Client whose call() never leaves the
    process. A run is served `page_records` records at a page, as the API pages a large one, and
    a page stops at `page_bytes` encoded, as the API's does (always one entry, marked oversized
    when it alone is over). from=latest answers where a first start reads from, reading no run.
    A query the API's OpenAPI refuses raises Unprocessable. `calls` records every (method, path,
    query or body)."""

    def __init__(
        self, entries: list[dict[str, Any]], page_records: int = 10**6, page_bytes: int = 8 << 20
    ) -> None:
        super().__init__("https://api.test", "k")
        self.entries = entries
        self.page_records = page_records
        self.page_bytes = page_bytes
        self.calls: list[tuple[str, str, Any]] = []

    def call(self, method: str, path: str, body: Any = None) -> Any:
        url = urllib.parse.urlparse(path)
        if method == "POST":
            self.calls.append((method, url.path, body))
            return None
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        self.calls.append((method, url.path, q))
        check_feed_query(q)
        if "cursor" in q:
            entry = next(e for e in self.entries if e["cursor"] == q["cursor"])
            return self._page(entry, int(q.get("page") or 0))
        if q.get("from") == "latest":
            return {"entries": [], "next": start(self.entries), "oldest": None}
        after, limit = q.get("after", ""), int(q.get("limit") or 20)
        found: list[dict[str, Any]] = []
        used = 0
        for e in [e for e in self.entries if e["cursor"] > after][:limit]:
            entry = self._page(e, 0)
            size = len(json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode())
            if found and used + size > self.page_bytes:
                break
            found.append({**entry, "oversized": True} if size > self.page_bytes else entry)
            used += size
        return {"entries": found, "next": found[-1]["cursor"] if found else after}

    def _page(self, entry: dict[str, Any], page: int) -> dict[str, Any]:
        if not entry["otlp"]["resourceLogs"]:
            return entry
        logs = entry["otlp"]["resourceLogs"][0]
        records = logs["scopeLogs"][0]["logRecords"]
        size = self.page_records
        part = {
            **logs,
            "scopeLogs": [
                {**logs["scopeLogs"][0], "logRecords": records[page * size : (page + 1) * size]}
            ],
        }
        more = page + 1 if (page + 1) * size < len(records) else None
        return {
            "cursor": entry["cursor"],
            "otlp": {"resourceLogs": [part]},
            **({"next_page": more} if more else {}),
        }
