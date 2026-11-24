"""The Open MCT plugin turns a run into a Plan: one group per antenna, one activity per pass,
rain-fade risks marked, and reads runs from Yamcs or straight from the Constellation feed. Run
under Node, against the shipped files."""

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import api
import pytest
import stub_api
from api import output, to_otlp

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_run_becomes_a_plan_per_antenna(tmp_path):
    shutil.copy(ROOT / "bridges/openmct/widget/constellation-forecast.js", tmp_path / "plugin.mjs")
    shutil.copy(ROOT / "bridges/shared/forecast-view.js", tmp_path / "forecast-view.js")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    item = {
        "entity": {"type": "pass", "id": "p1", "satellite_id": "sat-1", "antenna_id": "ant-1"},
        "target_at": "2026-11-06T10:00:00+00:00",
        "until": "2026-11-06T10:08:00+00:00",
        "value": {"p10": 2.0, "p50": 10.0, "p90": 11.0},
        "explain": {"expected_gb": {"value": {"p10": 2.0, "p50": 10.0, "p90": 11.0}}},
    }
    run = {
        "items": [
            item,
            {**item, "action": {"verb": "swap"}},
            {**item, "entity": {"type": "antenna", "id": "ant-1"}},
        ]
    }
    script = f"import {{ planBody }} from './plugin.mjs'; console.log(JSON.stringify(planBody({json.dumps(run)})))"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    plan = json.loads(out.stdout)
    assert list(plan) == ["ant-1"]
    first, risky = plan["ant-1"]
    assert first["name"] == "Forecast · sat-1 · 10.0 GB" and first["type"] == "forecast-pass"
    assert first["end"] - first["start"] == 8 * 60 * 1000
    assert risky["type"] == "rain-fade-risk" and risky["name"].endswith("rain-fade risk")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_follows_the_host_theme_from_its_text_colour(tmp_path):
    shutil.copy(ROOT / "bridges/openmct/widget/constellation-forecast.js", tmp_path / "plugin.mjs")
    shutil.copy(ROOT / "bridges/shared/forecast-view.js", tmp_path / "forecast-view.js")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = (
        "import { isDark } from './plugin.mjs';"
        "console.log(JSON.stringify(['rgb(230, 230, 230)', 'rgb(20, 20, 20)', 'rgba(0, 0, 0, 0)'].map(isDark)))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == [False, True, True]


def _node(tmp_path, script: str):
    shutil.copy(ROOT / "bridges/openmct/widget/constellation-forecast.js", tmp_path / "plugin.mjs")
    shutil.copy(ROOT / "bridges/shared/forecast-view.js", tmp_path / "forecast-view.js")
    (tmp_path / "package.json").write_text('{"type": "module"}')
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


PASS = {
    "entity": {"type": "pass", "id": "p1", "satellite_id": "sat-1", "antenna_id": "ant-1"},
    "target_at": "2099-11-06T10:00:00+00:00",
    "until": "2099-11-06T10:08:00+00:00",
    "value": {"p10": 2.0, "p50": 10.0, "p90": 11.0},
    "explain": {"expected_gb": {"value": {"p10": 2.0, "p50": 10.0, "p90": 11.0}}},
}
ACTION = {
    "verb": "swap",
    "to": "p2",
    "to_antenna_id": "ant-2",
    "to_at": "2099-11-06T11:00:00+00:00",
    "deadline": "2099-11-06T09:45:00+00:00",
    "gain_gb_p10": 4.0,
}
APPROVED = {**PASS, "action": {**ACTION, "mode": "approve"}, "action_status": "approved"}
ADVICE = {**PASS, "entity": {**PASS["entity"], "id": "p3"}, "action": {**ACTION, "mode": "suggest"}}
HELD = {**PASS, "entity": {**PASS["entity"], "id": "p4"}, "action_status": "pending"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_action_state_matches_the_bridges(tmp_path):
    cases = [
        PASS,
        ADVICE,
        APPROVED,
        HELD,
        {**PASS, "action_status": "rejected"},
        {**PASS, "action_status": "expired"},
    ]
    script = (
        "import { actionState } from './forecast-view.js';"
        f"console.log(JSON.stringify({json.dumps(cases)}.map(actionState)))"
    )
    assert _node(tmp_path, script) == [None, "advice", "approved", "pending", "rejected", "pending"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_plan_marks_approved_and_held_swaps_apart_from_advice(tmp_path):
    run = {"items": [APPROVED, ADVICE, HELD]}
    script = (
        "import { planBody } from './plugin.mjs';"
        f"console.log(JSON.stringify(planBody({json.dumps(run)})))"
    )
    approved, advice, held = _node(tmp_path, script)["ant-1"]
    assert approved["type"] == "rain-fade-approved" and approved["name"].endswith("swap approved")
    assert advice["type"] == "rain-fade-risk"
    assert held["type"] == "awaiting-approval" and held["name"].endswith("awaiting approval")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_puts_approved_swaps_first_and_counts_held_ones(tmp_path):
    run = {"kind": "rain-fade-swap", "workflow_version": 2}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {json.dumps(run)}, items = {json.dumps([ADVICE, APPROVED, HELD])};"
        "console.log(JSON.stringify([VIEWS.warnings(run, items), VIEWS.tiles(run, items)]))"
    )
    warnings, tiles = _node(tmp_path, script)
    assert warnings.index("Approved advice") < warnings.index("Rain-fade risk")
    assert "1 approved" in tiles and "1 awaiting approval" in tiles


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_words_a_swap_as_advice_with_the_real_loss_and_gain_never_a_cancel(tmp_path):
    run = {"kind": "rain-fade-swap", "workflow_version": 2}
    action = {
        **ACTION,
        "to_until": "2099-11-06T11:06:00+00:00",
        "to_gb": 4.0,
        "gain_gb": 1.5,
        "action": "move",
        "reason": "overlap",
    }
    noted = {**ADVICE, "action": action, "note": "Rain costs 50% of its dry 20.0 GB. Advice: ..."}
    script = (
        "import { VIEWS, advice } from './forecast-view.js';"
        f"const run = {json.dumps(run)}, items = {json.dumps([noted, ADVICE])};"
        "console.log(JSON.stringify([VIEWS.warnings(run, items), VIEWS.tiles(run, items), advice(items[0])]))"
    )
    warnings, tiles, words = _node(tmp_path, script)
    # The same words as the Python bridges' results.advice().
    assert words == (
        "Move to the backup on ant-2 at 2099-11-06 11:00Z to 11:06Z (4.0 GB) instead of this one,"
        " +1.5 GB: both passes use one radio at once"
    )
    assert "Rain costs 50% of its dry 20.0 GB." in warnings
    # Without the note's sentence, the p50 expected in the rain, never p10 against p50.
    assert "10.0 GB expected in the rain" in warnings and "keeps" not in warnings
    assert "Add a backup pass on ant-2" in warnings and "Decide by" in warnings
    assert "GB the advice adds" in tiles and "1.5" in tiles and "p50 minus p10" not in tiles
    assert "cancel" not in (warnings + tiles).lower()


def fake_openmct(plugins: list[str], installed_types: list[str]) -> str:
    """JavaScript for an Open MCT with these plugins and types: enough for install() to run, and
    `installed`, the plugins it installed."""
    return (
        "const installed = [];"
        f"const plugins = Object.fromEntries({json.dumps(plugins)}.map((n) => [n, () => n]));"
        "const openmct = { plugins, install: (p) => installed.push(p),"
        f" types: {{ listKeys: () => {json.dumps(installed_types)}, addType() {{}} }},"
        " objects: { addRoot() {}, addProvider() {} }, objectViews: { addProvider() {} },"
        " priority: { HIGH: 1 } };"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
@pytest.mark.parametrize(
    "plugins, types, expected",
    [
        # Open MCT 4.3.1 names its Plan plugin PlanLayout.
        (["PlanLayout", "Timeline"], [], ["PlanLayout", "Timeline"]),
        # Other versions name it Plan.
        (["Plan", "Timeline"], [], ["Plan", "Timeline"]),
        (["Plan", "PlanLayout", "Timeline"], [], ["PlanLayout", "Timeline"]),
        # A page that installed them already gets them once.
        (["PlanLayout", "Timeline"], ["plan", "time-strip"], []),
    ],
)
def test_the_plugin_installs_open_mcts_plan_by_whichever_name_it_has(tmp_path, plugins, types, expected):
    script = (
        "import ConstellationForecast from './plugin.mjs';"
        + fake_openmct(plugins, types)
        + "ConstellationForecast({ source: 'api', token: 'ro' })(openmct);"
        "console.log(JSON.stringify(installed))"
    )
    assert _node(tmp_path, script) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_never_shows_a_held_action_with_its_body_as_a_swap_to_decide(tmp_path):
    run = {"kind": "rain-fade-swap", "workflow_version": 2}
    held = {**ADVICE, "action_status": "pending"}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {json.dumps(run)}, items = {json.dumps([held])};"
        "console.log(JSON.stringify(VIEWS.warnings(run, items)))"
    )
    assert "Rain-fade risk" not in _node(tmp_path, script)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_shows_a_pass_at_risk_with_no_backup_as_advice_with_nothing_to_decide(tmp_path):
    run = {"kind": "rain-fade-swap", "workflow_version": 2}
    advisory = {**PASS, "severity": "warning", "note": "No backup pass in the window"}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {json.dumps(run)}, items = {json.dumps([advisory])};"
        "console.log(JSON.stringify([VIEWS.warnings(run, items), VIEWS.tiles(run, items)]))"
    )
    warnings, tiles = _node(tmp_path, script)
    assert "Rain-fade risk" in warnings and "No backup pass in the window" in warnings
    assert "No swaps to decide" not in warnings
    assert '<div class="cf-tile-value">0</div><div class="cf-tile-label">swaps to decide' in tiles


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_escapes_every_value_a_forecast_carries(tmp_path):
    # Every string a bridge copies from a forecast doc, hostile; no block may render it as markup.
    bad = '<img src=x onerror=alert(1)>'
    gb = {"p10": 2.0, "p50": 10.0, "p90": 11.0, "passes": bad, "sellable_min": 5}
    item = {
        "entity": {"type": "pass", "id": bad, "satellite_id": bad, "antenna_id": bad,
                   "ground_station_id": bad},
        "target_at": bad,
        "until": bad,
        "explain": {
            "expected_gb": {"value": gb, "unit": bad, "source": bad},
            bad: {"value": 1.0, "unit": bad, "source": bad},
            "ledger": {"value": {"status": bad, "dead": True, "delivered_gb": 1, "possible_gb": 2}},
        },
        "series": [{"t": PASS["target_at"], "value": {"sinr_db": 3.0, "modcod": bad}}],
        "action": {**ACTION, "to_antenna_id": bad, "to_at": bad},
        "action_status": "approved",
        "workflow_version": bad,
    }
    antenna = {**item, "entity": {"type": "antenna", "id": bad}, "target_at": "2099-11-06" + bad}
    kinds = ["pass-yield", "modcod-plan", "antenna-capacity", "rain-fade-swap", "audit-ledger", "rfi-watch"]
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const items = {json.dumps([item])}, antenna = {json.dumps([antenna])}, bad = {json.dumps(bad)};"
        f"console.log(JSON.stringify({json.dumps(kinds)}.flatMap((kind) => {{"
        "  const run = { kind, workflow: bad, workflow_version: bad, run_id: bad };"
        "  const its = kind === 'antenna-capacity' ? antenna : items;"
        "  return Object.values(VIEWS).map((view) => view(run, its, bad));"
        "})))"
    )
    for html in _node(tmp_path, script):
        assert "<img" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_pass_shows_its_expected_gb_whatever_node_id_its_ledger_uses(tmp_path):
    gb = {"p10": 2.0, "p50": 10.0, "p90": 11.0}
    copied = {**PASS, "value": gb, "explain": {"gb_node": {"value": gb}}}
    script = (
        "import { planBody } from './plugin.mjs'; import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: 'pass-yield', items: {json.dumps([copied])} }};"
        "console.log(JSON.stringify([planBody(run), VIEWS.list(run, run.items)]))"
    )
    plan, rows = _node(tmp_path, script)
    assert plan["ant-1"][0]["name"] == "Forecast · sat-1 · 10.0 GB"
    assert ">10.0</td>" in rows


class PagedApi(api.ResultsApi):
    """The feed, serving at most `most` records a page, fewer if asked, as the API pages."""

    most = 10**6

    def call(self, method, path, body=None):
        asked = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get("page_records")
        self.page_records = min(self.most, int(asked[0])) if asked else 10**6
        return super().call(method, path, body)


def serve(feed: PagedApi, later: list) -> str:
    """`feed` over HTTP; GET /_publish adds the entries in `later`, as runs finishing."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/_publish":
                feed.entries += later
                feed.calls = []
                body = {}
            else:
                assert self.headers["x-api-key"] == "ro"
                body = feed.call("GET", self.path)
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def feed_entry(cursor, workflow, connection, items):
    run = {"workflow": workflow, "run_id": cursor, "template": "pass-yield"}
    made = datetime(2026, 11, 5, tzinfo=UTC)
    return {"cursor": cursor, "otlp": to_otlp("t", items, made, run, output(connection))}


def passes(n, p50):
    return [
        {**PASS, "entity": {**PASS["entity"], "id": f"p{i}"}, "value": {**PASS["value"], "p50": p50}}
        for i in range(n)
    ]


def read_feed(tmp_path, feed: PagedApi, later: list | None = None) -> dict:
    """Loads index.json and every workflow from `feed`, publishes `later`, and loads them again."""
    script = f"""
import {{ feedLoader }} from './plugin.mjs';
const api = {json.dumps(serve(feed, later or []))};
const load = feedLoader({{ apiUrl: api + '/', token: 'ro' }});
const read = async () => {{
  const index = await load('index.json');
  const runs = {{}};
  for (const w of ['v', 'w', 'x']) {{
    try {{ runs[w] = await load(`workflow-${{w}}.json`); }} catch (e) {{ runs[w] = e.message; }}
  }}
  return {{ index: index.workflows.map((w) => w.workflow), runs }};
}};
const first = await read();
await fetch(api + '/_publish');
console.log(JSON.stringify({{ first, then: await read() }}));
"""
    return _node(tmp_path, script)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_api_source_reads_only_the_newest_run_of_each_workflow_whole(tmp_path):
    old, newer, v, latest = passes(3, 1.0), passes(3, 12.0), passes(5, 7.0), passes(4, 20.0)
    feed = PagedApi(
        [
            feed_entry("c1#r1", "w", "openmct", old),
            feed_entry("c2#r2", "w", "openmct", newer),
            feed_entry("c3#r3", "x", "yamcs-bridge", passes(1, 5.0)),
            feed_entry("c4#r4", "v", "openmct", v),
        ]
    )
    feed.most = 2
    first_calls = feed.calls
    got = read_feed(tmp_path, feed, [feed_entry("c5#r5", "w", "openmct", latest)])
    first, then = got["first"], got["then"]
    assert first["index"] == ["v", "w"] and first["runs"]["x"] == "none yet"
    assert first["runs"]["w"]["run_id"] == "c2#r2" and first["runs"]["w"]["items"] == newer
    assert first["runs"]["v"]["items"] == v and first["runs"]["v"]["count"] == 5
    assert first["runs"]["v"]["blocks"] and "items" not in str(first["index"])
    for _, _, query in first_calls + feed.calls:
        api.check_feed_query(query)
    # It finds each workflow's newest run from one record of every entry, then reads only those
    # whole, joining their pages; the older run of w is never read whole.
    scans = [q for _, _, q in first_calls if q.get("page_records") == "1"]
    assert scans and all(q["consumer"] == "openmct" and q["limit"] == "20" for q in scans)
    wholes = [q.get("after", q.get("cursor")) for _, _, q in first_calls if q.get("page_records") != "1"]
    assert sorted(wholes) == ["c1#r1", "c2#r2", "c3#r3", "c4#r4", "c4#r4"]
    # A new run: read from where it stopped, and only the new run whole.
    assert then["runs"]["w"]["run_id"] == "c5#r5" and then["runs"]["w"]["items"] == latest
    assert then["runs"]["v"]["items"] == v
    assert all(q.get("after", q.get("cursor")) >= "c4#r4" for _, _, q in feed.calls)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_api_source_refuses_a_run_too_large_for_a_browser_and_reads_on(tmp_path):
    feed = PagedApi(
        [
            feed_entry("c1#r1", "w", "openmct", passes(51, 1.0)),
            feed_entry("c2#r2", "v", "openmct", passes(1, 7.0)),
        ]
    )
    feed.most = 1
    got = read_feed(tmp_path, feed)
    assert "more than" in got["first"]["runs"]["w"] and got["first"]["runs"]["v"]["count"] == 1
    assert got["then"]["runs"]["w"] == got["first"]["runs"]["w"]
    # Read once: the refresh does not read the large run again.
    assert not [q for _, _, q in feed.calls if q.get("page_records") != "1"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_api_source_stops_on_a_refused_key_and_backs_off_long_when_limited(tmp_path):
    script = """
import { feedLoader } from './plugin.mjs';
let clock = 0;
const answers = { refused: [401], limited: [429, 429, 402, 200] };
const run = async (name) => {
  const statuses = answers[name];
  let asked = 0;
  const fetchFn = async () => {
    asked++;
    const status = statuses.shift() ?? 200;
    const body = status === 429 ? { detail: { error: 'auth_lockout' } } : { entries: [] };
    return { ok: status === 200, status, json: async () => body };
  };
  const load = feedLoader({ token: 'k', fetchFn, now: () => clock });
  const seen = [];
  // Every 15 s for an hour, as the view refreshes.
  for (clock = 0; clock <= 3600_000; clock += 15_000) {
    try { await load('index.json'); seen.push('ok'); } catch (e) { seen.push(e.message); }
  }
  return { asked, first: seen[0], last: seen.at(-1), oks: seen.filter((s) => s === 'ok').length };
};
console.log(JSON.stringify({ refused: await run('refused'), limited: await run('limited') }));
"""
    got = _node(tmp_path, script)
    refused, limited = got["refused"], got["limited"]
    assert refused["asked"] == 1 and "401" in refused["first"] and "results:read" in refused["first"]
    assert "reload" in refused["first"] and refused["last"] == refused["first"]
    # 429, 429 and 402 each pause it longer (1, 2, then 4 minutes), then it reads again.
    assert limited["asked"] == 3 + limited["oks"]
    assert "auth_lockout" in limited["first"] and "again in 1 min" in limited["first"]
    assert limited["oks"] == 241 - (4 + 8 + 16) and limited["last"] == "ok"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_yamcs_source_needs_a_yamcs_url(tmp_path):
    script = """
import ConstellationForecast from './plugin.mjs';
globalThis.location = { origin: 'http://mct.test' };
let refused = null;
try { ConstellationForecast(); } catch (e) { refused = e.message; }
console.log(JSON.stringify([refused, typeof ConstellationForecast({ yamcsUrl: 'http://yamcs.test:8090' })]));
"""
    refused, made = _node(tmp_path, script)
    assert "yamcsUrl" in refused and made == "function"


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub_api.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def ask(url: str, method: str = "GET", **headers: str) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers)) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


FOREIGN = "https://mct.customer.example"


def test_the_stub_answers_any_origin_on_the_feed_read_only_without_credentials(stub):
    status, headers, _ = ask(
        f"{stub}/predictions/feed?consumer=openmct", Origin=FOREIGN, **{"x-api-key": "k"}
    )
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in headers
    preflight = {"Origin": FOREIGN, "Access-Control-Request-Headers": "x-api-key"}
    status, headers, _ = ask(
        f"{stub}/predictions/feed", "OPTIONS", **preflight, **{"Access-Control-Request-Method": "GET"}
    )
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"
    status, _, body = ask(
        f"{stub}/predictions/feed", "OPTIONS", **preflight, **{"Access-Control-Request-Method": "POST"}
    )
    assert (status, body) == (400, b"Disallowed CORS method")


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/predictions/feed/checkin", "POST"),
        ("/predictions/feed/deliveries", "POST"),
        ("/topology/config", "GET"),
    ],
)
def test_the_stub_refuses_a_foreign_origin_everywhere_else(stub, path, method):
    asked = {"Origin": FOREIGN, "Access-Control-Request-Method": method}
    status, headers, body = ask(f"{stub}{path}", "OPTIONS", **asked)
    assert (status, body) == (400, b"Disallowed CORS origin")
    status, headers, _ = ask(f"{stub}{path}", Origin=FOREIGN)
    assert "Access-Control-Allow-Origin" not in headers


def test_the_stub_refuses_a_feed_query_the_openapi_does_not_allow(stub):
    status, _, _ = ask(f"{stub}/predictions/feed?limit=50", **{"x-api-key": "k"})
    assert status == 422
    assert api.FEED_QUERY["limit"]["maximum"] == 20


ESBUILD = ROOT / "clients/typescript/node_modules/.bin/esbuild"


@pytest.mark.skipif(not ESBUILD.exists(), reason="needs clients/typescript npm ci")
def test_the_single_file_bundle_sets_a_global_an_open_mct_page_installs(tmp_path):
    subprocess.run([ROOT / "bridges/openmct/widget/build.sh", tmp_path], check=True)
    script = (
        "globalThis.window = globalThis; globalThis.location = { origin: 'http://x' };"
        f"require({json.dumps(str(tmp_path / 'constellation-openmct.js'))});"
        "const install = window.ConstellationForecast({ source: 'api', token: 'ro' });"
        "console.log(typeof install)"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "function"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_list_shows_a_pass_max_elevation_from_the_orbit_ledger_entry(tmp_path):
    # The ledger keys each entry by the node that wrote it: the input node writes elevation.
    item = {**PASS, "explain": {**PASS["explain"], "passes": {"value": 63.4, "unit": "deg", "source": "orbit"}}}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: 'pass-yield' }}, items = {json.dumps([item])};"
        "console.log(JSON.stringify(VIEWS.list(run, items)))"
    )
    assert "63°" in _node(tmp_path, script)


RFI = {
    "entity": {"type": "link", "id": "ant-2:sat-1", "satellite_id": "sat-1", "antenna_id": "ant-2"},
    "target_at": "2099-11-06T10:00:00Z",
    "until": "2099-11-06T11:00:00+00:00",
    "value": {"p10": 9.5, "p50": 11.0, "p90": 12.0, "lock_loss_risk": 0.25},
    "explain": {"links": {"value": 80.0, "unit": "deg", "source": "orbit"}},
    "severity": "critical",
    "action": {"verb": "reroute", "from": "ant-2:sat-1", "mode": "suggest"},
}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_an_rfi_watch_run_shows_rfi_in_db_and_its_reroutes_never_as_gb_or_swaps(tmp_path):
    approved = {**RFI, "entity": {**RFI["entity"], "id": "ant-1:sat-1"}, "action_status": "approved",
                "decision_id": "d1", "value": {**RFI["value"], "lock_loss_risk": 0.0}, "severity": "warning"}
    held = {**RFI, "entity": {**RFI["entity"], "id": "ant-3:sat-1"}, "action_status": "pending"}
    del held["action"]
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: 'rfi-watch', workflow: 'w', workflow_version: 2, run_id: 'r' }}, items = {json.dumps([RFI, approved, held])};"
        "console.log(JSON.stringify(Object.fromEntries(Object.entries(VIEWS).map(([k, f]) => [k, f(run, items, null)]))))"
    )
    views = _node(tmp_path, script)
    html = " ".join(views.values())
    assert "GB" not in html and "swap" not in html.lower()
    assert "11.0 dB" in views["tiles"] and "1 awaiting approval" in views["tiles"]
    assert views["warnings"].index("Approved: carry out") < views["warnings"].index("Reroute advised")
    assert "25%" in views["list"] and "11.0" in views["list"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
@pytest.mark.parametrize("kind", ["pass-yield", "antenna-capacity", "rfi-watch"])
def test_every_view_shows_one_typical_value_never_a_p10_or_p90(tmp_path, kind):
    # Models have no spread yet: the view shows the p50 alone, never its p10 (2.0) or p90 (11.0).
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    item = {**(RFI if kind == "rfi-watch" else PASS), "target_at": soon}
    if kind == "antenna-capacity":
        gb = {**PASS["value"], "passes": 9, "sellable_min": 60}
        item = {"entity": {"type": "antenna", "id": "ant-1"}, "target_at": soon, "value": gb,
                "explain": {"expected_gb": {"value": gb}}}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: '{kind}', workflow: 'w', workflow_version: 2, run_id: 'r' }}, items = {json.dumps([item])};"
        "console.log(JSON.stringify(Object.values(VIEWS).map((f) => f(run, items, null)).join(' ')))"
    )
    html = _node(tmp_path, script)
    low, typical, high = (item["value"][q] for q in ("p10", "p50", "p90"))
    assert f"{typical:.1f}" in html
    assert not re.search(r"p10|p90|9 times out of 10", html, re.IGNORECASE)
    assert f">{low:.1f}<" not in html and f"{high:.1f}" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_gb_chart_axis_reaches_the_highest_hour(tmp_path):
    # 250 GB in the next hour: an axis stepping by 100 must go to 300, not stop at 200.
    soon = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(minutes=30)
    gb = {"p10": 250.0, "p50": 250.0, "p90": 250.0}
    item = {**PASS, "target_at": soon.isoformat(), "value": gb, "explain": {}}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"console.log(JSON.stringify(VIEWS.chart({{kind: 'pass-yield'}}, {json.dumps([item])})))"
    )
    axis = re.findall(r'class="cf-axis" x="\d+" y="[\d.]+" text-anchor="end">([^<]+)<', _node(tmp_path, script))
    assert axis[-1] == "300"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_run_whose_swaps_are_all_held_says_so_once_and_never_no_risk(tmp_path):
    run = {"kind": "rain-fade-swap", "workflow_version": 2}
    held = [{**HELD, "entity": {**HELD["entity"], "id": f"p{n}"}} for n in range(3)]
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {json.dumps(run)}, items = {json.dumps(held)};"
        "console.log(JSON.stringify([VIEWS.chart(run, items), VIEWS.warnings(run, items)]))"
    )
    chart, warnings = _node(tmp_path, script)
    assert "No pass is at risk" not in chart and "3 passes at risk awaiting approval in Constellation" in chart
    assert warnings.count("awaiting approval in Constellation") == 1


def _steps(*modcods: str) -> list[dict]:
    return [{"t": PASS["target_at"], "value": {"modcod": m, "sinr_db": 3.0}} for m in modcods]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_a_modcod_plan_counts_changes_as_the_cosmos_and_lattice_bridges_do(tmp_path):
    # The series holds each MODCOD switch, starting with the one at AOS: changes are the steps
    # after it, so a plan up and back down through the same MODCODs is not "N MODCODs".
    there_and_back = {**PASS, "series": _steps("QPSK 1/2", "8PSK 2/3", "16APSK 3/4", "8PSK 2/3")}
    one = {**PASS, "entity": {**PASS["entity"], "id": "p2"}, "series": _steps("QPSK 1/2", "8PSK 2/3")}
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: 'modcod-plan' }}, items = {json.dumps([there_and_back, one])};"
        "console.log(JSON.stringify(VIEWS.list(run, items)))"
    )
    rows = _node(tmp_path, script)
    assert "3 changes" in rows and "1 change<" in rows and "MODCODs" not in rows


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_ledger_shows_a_probability_or_share_as_a_percent_like_the_lock_loss_risk(tmp_path):
    explain = {
        **PASS["explain"],
        "success": {"value": 0.97, "unit": "probability", "source": "ssa"},
        "lock_loss_risk": {"value": 0.167, "unit": "share", "source": "acm"},
    }
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const run = {{ kind: 'pass-yield' }}, items = {json.dumps([{**PASS, 'explain': explain}])};"
        "console.log(JSON.stringify(VIEWS.ledger(run, items, 'p1')))"
    )
    ledger = _node(tmp_path, script)
    assert '<td class="cf-num">97%</td>' in ledger and '<td class="cf-num">17%</td>' in ledger
    assert "probability" not in ledger and "share" not in ledger


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_charts_label_gb_at_one_decimal_and_counts_as_whole_numbers(tmp_path):
    def day(antenna: str, p50: float) -> dict:
        gb = {"p10": p50 - 1, "p50": p50, "p90": p50 + 1, "passes": 9, "sellable_min": 60}
        return {"entity": {"type": "antenna", "id": antenna}, "target_at": "2099-11-06T00:00:00Z",
                "value": gb, "explain": {"expected_gb": {"value": gb}}}

    days = [day("ant-1", 164), day("ant-2", 96)]
    plans = [{**PASS, "series": _steps("QPSK 1/2", "8PSK 2/3", "QPSK 1/2")}]
    script = (
        "import { VIEWS } from './forecast-view.js';"
        f"const days = {json.dumps(days)}, plans = {json.dumps(plans)};"
        "console.log(JSON.stringify([VIEWS.chart({ kind: 'antenna-capacity' }, days),"
        " VIEWS.breakdown({ kind: 'antenna-capacity' }, days), VIEWS.chart({ kind: 'modcod-plan' }, plans)]))"
    )
    grid, ranked, mix = _node(tmp_path, script)
    for gb in (grid, ranked):
        assert ">164.0</text>" in gb and ">96.0</text>" in gb
    assert ">2</text>" in mix and ">1</text>" in mix and ".0</text>" not in mix


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_passes_tab_opens_on_pass_yield_whatever_order_the_index_lists_it_in(tmp_path):
    workflows = [
        {"workflow": "a-plan", "kind": "modcod-plan"},
        {"workflow": "ka-capacity", "kind": "antenna-capacity"},
        {"workflow": "z-yield", "kind": "pass-yield"},
        {"workflow": "z-yield-2", "kind": "pass-yield"},
    ]
    script = (
        "import { firstWorkflow } from './forecast-view.js';"
        f"const w = {json.dumps(workflows)};"
        "console.log(JSON.stringify([firstWorkflow(w), firstWorkflow(w, 'passes'),"
        " firstWorkflow(w, 'capacity'), firstWorkflow(w, 'audit')]))"
    )
    first, passes, capacity, audit = _node(tmp_path, script)
    assert first["workflow"] == passes["workflow"] == "z-yield"
    assert capacity["workflow"] == "ka-capacity" and audit is None
