"""The Constellation API and Lattice, over HTTP, for the install tests (install.yml): one
pass-yield run in the feed for each of the yamcs-bridge, openmct and lattice-bridge connections,
the topology config, and the Lattice routes the bridge calls. CORS is the API's: any origin may
GET /predictions/feed without credentials, as the Open MCT plugin does from a browser, and every
other route answers only the platform's origin. A feed query the API's OpenAPI refuses is a 422.
GET /_calls lists every call made, for the checks.

    python tests/stub_api.py [port]
"""

import json
import sys
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from api import ResultsApi, Unprocessable, item, output, to_otlp

CONNECTIONS = {"yamcs-bridge": None, "openmct": None, "lattice-bridge": {"system": "lattice"}}
DOCUMENT = {
    "ground_stations": {
        "gs-1": {
            "latitude_deg": 52.0,
            "longitude_deg": 4.4,
            "altitude_m": 10.0,
            "antennas": {"ant-1": {"min_elevation_deg": 10}},
        }
    },
    "satellites": {"sat-1": {"norad_id": 1}},
}


def feed() -> dict[str, dict[str, Any]]:
    """Each connection's feed entry: a pass an hour from now, 10 GB expected, the run the whole
    answer for the next day."""
    made = datetime.now(UTC)
    at = made + timedelta(hours=1)
    gb = {"p10": 2.0, "p50": 10.0, "p90": 11.0}
    passed = item(
        {
            "type": "pass",
            "id": "pass_1",
            "link_id": "sat-1|ant-1",
            "satellite_id": "sat-1",
            "ground_station_id": "gs-1",
            "antenna_id": "ant-1",
            "source": "orbit",
        },
        at.isoformat(),
        gb,
        until=(at + timedelta(minutes=8)).isoformat(),
        explain={"expected_gb": {"value": gb, "unit": "GB", "source": "expected_gb"}},
        workflow_version=1,
    )
    entries = {}
    for n, (connection, mapping) in enumerate(CONNECTIONS.items(), 1):
        run = {
            "workflow": "pass-yield",
            "run_id": f"run_{n}",
            "template": "pass-yield",
            "window_from": made.isoformat(),
            "window_to": (made + timedelta(days=1)).isoformat(),
        }
        otlp = to_otlp("t", [passed], made, run, output(connection, mapping))
        entries[connection] = {"cursor": f"c{n}#run_{n}", "otlp": otlp}
    return entries


ENTRIES = feed()
CALLS: list[tuple[str, str]] = []
# As the API's CORS: the configured origins (the platform's) on every route, any origin on the
# results feed, GET only and never with credentials.
ORIGINS = {"https://platform.constellation.space"}
OPEN_READ = "/predictions/feed"
HEADERS = ["authorization", "x-api-key", "content-type", "x-constellation-tenant", "idempotency-key"]
SAFELISTED = ["Accept", "Accept-Language", "Content-Language", "Content-Type"]


class Handler(BaseHTTPRequestHandler):
    def _open(self) -> bool:
        return urllib.parse.urlparse(self.path).path == OPEN_READ

    def send(self, status: int, body: Any = None, text: str | None = None) -> None:
        raw = text.encode() if text is not None else b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and self._open():
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "text/plain" if text is not None else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        """A CORS preflight, answered as the API's middleware does: 400 with what it refuses."""
        origin = self.headers.get("Origin")
        method = self.headers.get("Access-Control-Request-Method")
        if not origin or not method:
            return self.send(405, {"detail": "Method Not Allowed"})
        methods = ["GET"] if self._open() else ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
        asked = self.headers.get("Access-Control-Request-Headers") or ""
        refused = [
            what
            for what, ok in (
                ("origin", self._open() or origin in ORIGINS),
                ("method", method in methods),
                (
                    "headers",
                    all(
                        h.strip().lower() in HEADERS + [s.lower() for s in SAFELISTED]
                        for h in asked.split(",")
                        if h.strip()
                    ),
                ),
            )
            if not ok
        ]
        if refused:
            return self.send(400, text=f"Disallowed CORS {', '.join(refused)}")
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*" if self._open() else origin)
        self.send_header("Access-Control-Allow-Methods", ", ".join(methods))
        self.send_header("Access-Control-Allow-Headers", ", ".join(SAFELISTED + HEADERS))
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        CALLS.append(("GET", url.path))
        if url.path == "/_calls":
            return self.send(200, CALLS)
        if url.path == "/predictions/feed":
            if not self.headers.get("x-api-key"):
                return self.send(401, {"detail": "no API key"})
            consumer = urllib.parse.parse_qs(url.query).get("consumer", [""])[0]
            mine = [ENTRIES[consumer]] if consumer in ENTRIES else []
            try:
                return self.send(200, ResultsApi(mine).call("GET", self.path))
            except Unprocessable as exc:
                return self.send(422, {"detail": [{"msg": str(exc)}]})
        if url.path == "/topology/config":
            return self.send(200, {"document": DOCUMENT})
        if url.path.startswith("/api/v1/objects"):
            return self.send(200, {"path_metadatas": []})
        self.send(404, {"detail": "not in the stub"})

    def do_POST(self) -> None:
        self.write("POST")

    def do_PUT(self) -> None:
        self.write("PUT")

    def write(self, method: str) -> None:
        url = urllib.parse.urlparse(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        CALLS.append((method, url.path))
        if url.path == "/api/v1/agent/stream":
            # Lattice holds the stream open; this one stays quiet until the bridge gives up on it.
            self.send_response(200)
            self.end_headers()
            time.sleep(55)
            return
        self.send(200, {})

    def log_message(self, *args: Any) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(
        ("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), Handler
    ).serve_forever()
