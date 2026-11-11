"""The bridge template (constellation_space.bridges.template): it applies each run once, keeps its
cursor so a restart resumes where it stopped, acts only on approved actions and each only once
(across runs and restarts), logs every item applied for the conformance check, and stops on a
refused key or plan, as the shipped bridges do."""

import io
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message

import pytest
from api import ResultsApi, item, output, to_otlp
from constellation_space.bridges.template import Bridge
from constellation_space.client import Client

MANIFEST = {"name": "my-bridge", "title": "My system", "settings": {}}
SWAP = {"verb": "swap", "from": "p", "to": "q", "deadline": "2030-01-01T00:00:00+00:00"}
PASS = {"type": "pass", "id": "p"}


def run(cursor: str, *items: dict) -> dict:
    run_id = cursor.split("#", 1)[1]
    feed_output = output("my-bridge", {"system": "custom", "settings": {"label": "x"}})
    otlp = to_otlp(
        "t",
        list(items),
        datetime(2030, 1, 1, tzinfo=UTC),
        {"workflow": "w", "run_id": run_id},
        feed_output,
    )
    return {"cursor": cursor, "otlp": otlp}


def passes(*states: tuple[str, dict]) -> list[dict]:
    return [
        item({**PASS, "id": name}, "2030-01-01T01:00:00+00:00", {"p50": 1.0}, **facets)
        for name, facets in states
    ]


ENTRIES = [
    run("1#run_1", *passes(("advice", {"action": {**SWAP, "mode": "suggest"}}), ("plain", {}))),
    run(
        "2#run_2",
        *passes(
            ("approved", {"action": {**SWAP, "mode": "approve"}, "action_status": "approved"}),
            ("pending", {"action_status": "pending"}),
            ("rejected", {"action_status": "rejected"}),
            ("unknown", {"action_status": "expired"}),
        ),
    ),
]


class Recorder(Bridge):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.applied: list[tuple[list[str], dict, dict]] = []
        self.acted: list[str] = []
        self.fail_once = False

    def apply(self, items, settings, run):
        self.applied.append(([i["entity"]["id"] for i in items], settings, run))
        return {"passes": len(items)}

    def act(self, item, settings, run):
        self.acted.append(item["entity"]["id"])
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("the system went down after acting")
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "actions.jsonl"
    monkeypatch.setenv("CONSTELLATION_CONFORMANCE_LOG", str(log))
    # These tests replay the whole feed; a first start from the head is tested on its own below.
    monkeypatch.setenv("BACKFILL", "1")
    return tmp_path / "cursor.txt", log


def actions(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def acted(log) -> list[str]:
    return [line["item_id"] for line in actions(log) if line["acted"]]


APPROVED_AGAIN = run(
    "3#run_3",
    *passes(("approved", {"action": {**SWAP, "mode": "approve"}, "action_status": "approved"})),
)


def test_each_run_is_applied_and_reported_and_only_the_approved_action_is_acted_on(env):
    cursor_file, log = env
    api = ResultsApi(ENTRIES, page_records=1)
    bridge = Recorder(MANIFEST, client=api, cursor_file=cursor_file)
    bridge.poll()
    assert [ids for ids, _, _ in bridge.applied] == [
        ["advice", "plain"],
        ["approved", "pending", "rejected", "unknown"],
    ]
    assert bridge.applied[0][1] == {"label": "x"} and bridge.applied[0][2]["run_id"] == "run_1"
    assert bridge.acted == ["approved"]
    assert actions(log) == [
        {"item_id": "advice", "state": "advice", "acted": False, "verb": "swap"},
        {"item_id": "plain", "state": "forecast", "acted": False, "verb": None},
        {"item_id": "approved", "state": "approved", "acted": True, "verb": "swap"},
        {"item_id": "pending", "state": "pending", "acted": False, "verb": None},
        {"item_id": "rejected", "state": "rejected", "acted": False, "verb": None},
        {"item_id": "unknown", "state": "pending", "acted": False, "verb": None},
    ]
    reports = [b for m, p, b in api.calls if p == "/predictions/feed/deliveries"]
    assert [(r["run_id"], r["applied"]) for r in reports] == [
        ("run_1", {"passes": 2}),
        # The approval it carried out, counted for the platform to show.
        ("run_2", {"passes": 4, "acted": 1}),
    ]
    assert cursor_file.read_text() == "2#run_2"


def test_a_restart_resumes_from_the_saved_cursor_without_acting_again(env):
    cursor_file, log = env
    Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=cursor_file).poll()
    api = ResultsApi(ENTRIES + [run("3#run_3", *passes(("later", {})))])
    restarted = Recorder(MANIFEST, client=api, cursor_file=cursor_file)
    assert restarted.cursor == "2#run_2"
    restarted.poll()
    assert [ids for ids, _, _ in restarted.applied] == [["later"]] and restarted.acted == []
    assert acted(log) == ["approved"]
    assert api.calls[0][2]["after"] == "2#run_2"


def test_a_run_that_fails_keeps_its_cursor_and_is_retried(env):
    cursor_file, log = env
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES[1:]), cursor_file=cursor_file)
    bridge.fail_once = True
    with pytest.raises(RuntimeError):
        bridge.poll()
    assert not cursor_file.exists() and actions(log) == []
    bridge.acted.clear()
    bridge.poll()
    assert bridge.acted == ["approved"] and cursor_file.read_text() == "2#run_2"


def test_an_action_done_before_a_later_failure_is_not_done_again(env, monkeypatch):
    cursor_file, log = env
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES[1:]), cursor_file=cursor_file)
    reports = iter([RuntimeError("report failed"), None])

    def report(*args):
        if failure := next(reports):
            raise failure

    monkeypatch.setattr("constellation_space.bridges.template.results.report", report)
    with pytest.raises(RuntimeError):
        bridge.poll()
    bridge.poll()
    assert bridge.acted == ["approved"] and acted(log) == ["approved"]
    # The retry reported what the first attempt wrote, without writing it again.
    assert [ids for ids, _, _ in bridge.applied] == [["approved", "pending", "rejected", "unknown"]]


class Replaying(ResultsApi):
    """A feed that, once, answers from its start whatever cursor it is asked after, as one
    replaying after a failover."""

    replayed = False

    def call(self, method, path, body=None):
        if "after=" in path and not self.replayed:
            self.replayed = True
            path = path.replace("after=", "ignored=")
        return super().call(method, path, body)


def test_a_run_the_feed_delivers_again_is_not_applied_again(env):
    cursor_file, log = env
    Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=cursor_file).poll()
    restarted = Recorder(
        MANIFEST, client=Replaying(ENTRIES + [APPROVED_AGAIN]), cursor_file=cursor_file
    )
    restarted.poll()
    assert [ids for ids, _, _ in restarted.applied] == [["approved"]]
    assert restarted.acted == []


def test_an_approved_action_sent_again_in_a_later_run_is_not_acted_on_again(env):
    cursor_file, log = env
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES + [APPROVED_AGAIN]), cursor_file=cursor_file)
    bridge.poll()
    assert bridge.acted == ["approved"] and acted(log) == ["approved"]
    assert actions(log)[-1] == {
        "item_id": "approved", "state": "approved", "acted": False, "verb": "swap"
    }


def test_an_approved_action_sent_again_after_a_restart_is_not_acted_on_again(env):
    cursor_file, log = env
    Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=cursor_file).poll()
    restarted = Recorder(MANIFEST, client=ResultsApi(ENTRIES + [APPROVED_AGAIN]), cursor_file=cursor_file)
    restarted.poll()
    assert restarted.acted == [] and acted(log) == ["approved"]
    assert (cursor_file.parent / "cursor.txt.acted").read_text() == (
        "approved|swap|q\t2030-01-08T01:00:00+00:00\n"
    )


def approval(cursor: str, decision: str) -> dict:
    facets = {"action": {**SWAP, "mode": "approve"}, "action_status": "approved"}
    return run(cursor, *passes(("approved", {**facets, "decision_id": decision})))


def test_a_standing_approval_is_acted_on_once_across_runs_and_restarts(env):
    cursor_file, log = env
    standing = [approval("1#run_1", "d1"), approval("2#run_2", "d1")]
    Recorder(MANIFEST, client=ResultsApi(standing), cursor_file=cursor_file).poll()
    restarted = Recorder(
        MANIFEST, client=ResultsApi(standing + [approval("3#run_3", "d1")]), cursor_file=cursor_file
    )
    restarted.poll()
    assert restarted.acted == [] and acted(log) == ["approved"]


def test_a_new_approval_of_the_same_action_is_acted_on_again(env):
    cursor_file, log = env
    entries = [approval("1#run_1", "d1"), approval("2#run_2", "d2")]
    bridge = Recorder(MANIFEST, client=ResultsApi(entries), cursor_file=cursor_file)
    bridge.poll()
    assert bridge.acted == ["approved", "approved"] and acted(log) == ["approved", "approved"]


def test_the_same_verb_to_another_target_is_another_action(env):
    cursor_file, log = env
    other = run(
        "3#run_3",
        *passes(
            ("approved", {"action": {**SWAP, "to": "r", "mode": "approve"}, "action_status": "approved"})
        ),
    )
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES + [other]), cursor_file=cursor_file)
    bridge.poll()
    assert bridge.acted == ["approved", "approved"]


def test_no_log_is_written_without_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSTELLATION_CONFORMANCE_LOG", raising=False)
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=tmp_path / "c")
    bridge.poll()
    assert bridge.acted == ["approved"]
    assert sorted(tmp_path.iterdir()) == [tmp_path / n for n in ("c", "c.acted", "c.api")]


OLD_APPROVAL = {"action": {**SWAP, "mode": "approve"}, "action_status": "approved"}


def test_a_first_start_reads_from_the_newest_run_and_never_acts_on_older_decisions(env, monkeypatch):
    cursor_file, log = env
    monkeypatch.delenv("BACKFILL")
    expired = {**OLD_APPROVAL, "action": {**SWAP, "deadline": "2020-01-01T00:00:00+00:00"}}
    feed = [
        approval("1#run_1", "d1"),
        run("2#run_2", *passes(("expired", {**expired, "decision_id": "d2"}), ("plain", {}))),
    ]
    for _ in range(3):
        Recorder(MANIFEST, client=ResultsApi(feed), cursor_file=cursor_file).poll()
    assert acted(log) == []
    assert [line["item_id"] for line in actions(log)] == ["expired", "plain"]


def test_backfill_applies_the_history_but_never_an_expired_action(env):
    cursor_file, log = env
    expired = {**OLD_APPROVAL, "action": {**SWAP, "deadline": "2020-01-01T00:00:00+00:00"}}
    feed = [approval("1#run_1", "d1"), run("2#run_2", *passes(("expired", expired)))]
    bridge = Recorder(MANIFEST, client=ResultsApi(feed), cursor_file=cursor_file)
    bridge.poll()
    assert bridge.acted == ["approved"] and acted(log) == ["approved"]


def test_an_approval_of_a_long_past_pass_is_acted_on_at_most_once_over_many_polls(env):
    cursor_file, log = env
    old = item(
        {**PASS, "id": "old"}, "2020-01-01T01:00:00+00:00", {"p50": 1.0},
        action={"verb": "swap", "to": "q", "mode": "approve"}, action_status="approved",
        decision_id="d-old",
    )
    feed = []
    bridge = Recorder(MANIFEST, client=ResultsApi(feed), cursor_file=cursor_file)
    for n in range(1, 4):
        feed.append(run(f"{n}#run_{n}", old))
        bridge.poll()
    assert bridge.acted == ["old"] and acted(log) == ["old"]


def test_a_malformed_item_is_skipped_and_counted_and_never_holds_up_its_run(env):
    cursor_file, log = env
    bad = {**passes(("bad", {}))[0], "target_at": "soon"}
    api = ResultsApi([run("1#run_1", bad, *passes(("good", {}))), run("2#run_2", *passes(("next", {})))])
    bridge = Recorder(MANIFEST, client=api, cursor_file=cursor_file)
    bridge.poll()
    assert [ids for ids, _, _ in bridge.applied] == [["good"], ["next"]]
    reports = [b["applied"] for m, p, b in api.calls if p == "/predictions/feed/deliveries"]
    assert reports == [{"passes": 1, "malformed": 1}, {"passes": 1}]
    assert cursor_file.read_text() == "2#run_2"


PLAN = json.dumps(
    {"detail": {"error": "plan_required", "plan": "enterprise", "feature": "output:custom"}}
).encode()


@pytest.mark.parametrize(
    "status, says", [(403, "plan does not include output:custom"), (401, "401")]
)
def test_a_refused_key_or_plan_stops_the_template_instead_of_polling_on(
    monkeypatch, capsys, tmp_path, status, says
):
    asked = []

    def urlopen(req, timeout=None):
        asked.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, status, "", Message(), io.BytesIO(PLAN))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        "constellation_space.bridges.template.time.sleep", lambda s: pytest.fail("polled again")
    )
    bridge = Recorder(MANIFEST, client=Client("https://api.test", "k"), cursor_file=tmp_path / "c")
    with pytest.raises(SystemExit) as stopped:
        bridge.run_forever()
    assert stopped.value.code == 2
    assert asked == ["https://api.test/predictions/feed/checkin"]
    (line,) = capsys.readouterr().err.splitlines()
    assert says in line


def test_the_template_checks_in_again_every_checkin_s(monkeypatch, tmp_path):
    """The platform reads a connection that stopped checking in as stopped."""
    api = ResultsApi([])
    bridge = Recorder(MANIFEST, client=api, cursor_file=tmp_path / "c")
    clock = [0.0]

    def sleep(seconds):
        clock[0] += 200
        if clock[0] > 500:
            raise SystemExit

    monkeypatch.setattr("constellation_space.bridges.template.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("constellation_space.bridges.template.time.sleep", sleep)
    with pytest.raises(SystemExit):
        bridge.run_forever()
    checkins = [p for m, p, _ in api.calls if p == "/predictions/feed/checkin"]
    assert len(checkins) == 2


@pytest.mark.parametrize("unsaved", ["cursor", "acted"])
def test_a_bridge_that_cannot_keep_its_state_stops_instead_of_repeating_its_work(
    env, monkeypatch, capsys, unsaved
):
    cursor_file, _ = env
    bridge = Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=cursor_file)

    def denied(*args):
        raise PermissionError(13, "Permission denied", str(cursor_file))

    if unsaved == "cursor":
        monkeypatch.setattr(bridge, "save_cursor", denied)
    else:
        monkeypatch.setattr(bridge._acted, "_save", denied)
    monkeypatch.setattr(
        "constellation_space.bridges.template.time.sleep", lambda s: pytest.fail("polled again")
    )
    with pytest.raises(SystemExit) as stopped:
        bridge.run_forever()
    assert stopped.value.code == 1
    assert "cannot keep its state" in capsys.readouterr().err
    assert len(bridge.applied) == (1 if unsaved == "cursor" else 2)


def test_a_cursor_read_from_another_api_is_never_resumed(env, capsys):
    cursor_file, _ = env
    mock = Client("http://localhost:7070", "")
    mock.call = ResultsApi(ENTRIES).call
    Recorder(MANIFEST, client=mock, cursor_file=cursor_file).poll()
    assert cursor_file.read_text() == "2#run_2"
    with pytest.raises(SystemExit) as stopped:
        Recorder(MANIFEST, client=ResultsApi(ENTRIES), cursor_file=cursor_file)
    assert "it now reads https://api.test" in str(stopped.value)
    assert "read http://localhost:7070 up to" in str(stopped.value)
    # The same API, spelled another way, resumes.
    same = Client("HTTP://LOCALHOST:7070/", "")
    assert Recorder(MANIFEST, client=same, cursor_file=cursor_file).cursor == "2#run_2"


def test_with_no_key_the_template_stops_at_once_except_against_the_mock(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CONSTELLATION_API_KEY", raising=False)
    bridge = Recorder(MANIFEST, client=Client("https://api.test", ""), cursor_file=tmp_path / "c")
    with pytest.raises(SystemExit) as stopped:
        bridge.run_forever()
    assert stopped.value.code == 2
    assert "CONSTELLATION_API_KEY is not set" in capsys.readouterr().err
    mock = Client("http://127.0.0.1:7070", "")
    mock.call = ResultsApi([]).call
    bridge = Recorder(MANIFEST, client=mock, cursor_file=tmp_path / "m")
    monkeypatch.setattr("constellation_space.bridges.template.time.sleep", lambda s: sys.exit(0))
    with pytest.raises(SystemExit) as polled:
        bridge.run_forever()
    assert polled.value.code == 0
