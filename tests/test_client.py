"""The API client (constellation_space.client): the key as x-api-key, retries on 429, 5xx and a dropped
connection, no retry on anything else, and workflow results read whole and in order."""

import http.client
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from pathlib import Path

import constellation_space
import pytest
from api import ResultsApi
from constellation_space import results
from constellation_space.client import ApiError, Client


class Answers:
    """urlopen answering each request with the next of `answers`: a status, or a JSON body."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            status, headers = answer
            message = Message()
            for k, v in headers.items():
                message[k] = v
            raise urllib.error.HTTPError(
                req.full_url, status, "", message, io.BytesIO(b'{"detail": "x"}')
            )
        return io.BytesIO(json.dumps(answer).encode())


@pytest.fixture
def wire(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("constellation_space.client.time.sleep", sleeps.append)

    def install(*answers):
        answers = Answers(*answers)
        monkeypatch.setattr(urllib.request, "urlopen", answers)
        return answers, sleeps

    return install


def test_the_key_and_url_come_from_the_environment(wire, monkeypatch):
    monkeypatch.setenv("CONSTELLATION_API_URL", "https://api.test/")
    monkeypatch.setenv("CONSTELLATION_API_KEY", " secret ")
    answers, _ = wire({"ok": True})
    assert Client().call("GET", "/x") == {"ok": True}
    (req,) = answers.requests
    assert req.full_url == "https://api.test/x" and req.get_header("X-api-key") == "secret"


@pytest.mark.parametrize(
    "url", ["http://localhost:7070", "http://127.0.0.1:7070", "http://[::1]:7070", "https://x.test"]
)
def test_the_key_goes_over_https_or_to_this_machine(wire, url):
    answers, _ = wire({})
    Client(url, "k").call("GET", "/x")
    assert answers.requests[0].get_header("X-api-key") == "k"


@pytest.mark.parametrize("url", ["http://10.0.0.5:7070", "http://example.com", "ftp://x.test"])
def test_the_key_never_goes_over_plain_http_to_another_machine(wire, url):
    answers, _ = wire({})
    with pytest.raises(ValueError, match="not sending the API key"):
        Client(url, "k").call("GET", "/x")
    assert answers.requests == []


def test_no_key_sends_no_header(wire, monkeypatch):
    monkeypatch.delenv("CONSTELLATION_API_KEY", raising=False)
    answers, _ = wire({})
    Client("http://example.com", "").call("GET", "/x")
    assert answers.requests[0].get_header("X-api-key") is None


def test_the_default_url_is_production(monkeypatch):
    monkeypatch.delenv("CONSTELLATION_API_URL", raising=False)
    assert Client().api_url == "https://api.constellation.space"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_busy_or_failing_api_is_retried_with_backoff(wire, status):
    answers, sleeps = wire((status, {}), (status, {}), {"ok": True})
    assert Client("https://api.test", "k").call("GET", "/x") == {"ok": True}
    assert len(answers.requests) == 3 and sleeps == [0.5, 1.0]


def test_retry_after_is_honored(wire):
    _, sleeps = wire((429, {"Retry-After": "7"}), {"ok": True})
    Client("https://api.test", "k").call("GET", "/x")
    assert sleeps == [7.0]


def test_a_long_retry_after_is_capped_and_logged(wire, capsys):
    _, sleeps = wire((503, {"Retry-After": "86400"}), {"ok": True})
    Client("https://api.test", "k").call("GET", "/x")
    assert sleeps == [60.0]
    assert "answered 503; retrying in 60 s (Retry-After)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("reset"),
        TimeoutError("The read operation timed out"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        ConnectionResetError(54, "Connection reset by peer"),
    ],
)
def test_a_dropped_or_timed_out_connection_is_retried(wire, failure):
    answers, _ = wire(failure, {"ok": True})
    assert Client("https://api.test", "k").call("GET", "/x") == {"ok": True}
    assert len(answers.requests) == 2


def test_a_refusal_says_why_and_the_fix():
    error = ApiError(401, b'{"error":"auth_failed"}', "https://api.constellation.space")
    assert str(error) == error.refusal and "https://platform.constellation.space/keys" in str(error)
    assert str(ApiError(404, b"{}")) == "Constellation answered 404: b'{}'"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_client_error_is_raised_at_once(wire, status):
    answers, sleeps = wire((status, {}))
    with pytest.raises(ApiError) as raised:
        Client("https://api.test", "k").call("GET", "/x")
    assert raised.value.status == status and len(answers.requests) == 1 and sleeps == []
    assert (raised.value.refusal is not None) == (status in (401, 403))


def test_retries_run_out(wire):
    answers, sleeps = wire(*[(503, {})] * 3)
    with pytest.raises(ApiError) as raised:
        Client("https://api.test", "k", retries=2).call("GET", "/x")
    assert raised.value.status == 503 and len(answers.requests) == 3 and sleeps == [0.5, 1.0]


def run(cursor: str, n: int) -> dict:
    records = [{"body": {"stringValue": f"{cursor}/{i}"}} for i in range(n)]
    return {
        "cursor": cursor,
        "otlp": {
            "resourceLogs": [
                {"resource": {"attributes": []}, "scopeLogs": [{"logRecords": records}]}
            ]
        },
    }


def test_results_are_every_run_after_the_cursor_each_joined_whole():
    api = ResultsApi([run("1#a", 1), run("2#b", 3), run("3#c", 2)], page_records=1)
    got = list(api.results("me", after="1#a"))
    assert [e["cursor"] for e in got] == ["2#b", "3#c"]
    assert [len(e["otlp"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"]) for e in got] == [3, 2]
    assert all("next_page" not in e for e in got)
    pulls = [q for _, _, q in api.calls if "after" in q]
    assert [q["after"] for q in pulls] == ["1#a", "2#b", "3#c"]
    assert all(q["consumer"] == "me" for _, _, q in api.calls)


def test_a_cursor_older_than_the_feed_holds_is_logged(capsys):
    client = Client("https://api.test", "k")
    client.call = lambda method, path, body=None: {"entries": [], "next": "", "oldest": "5#e"}
    assert list(client.results("me", after="1#a")) == []
    assert "no longer holds cursor 1#a" in capsys.readouterr().err
    assert list(client.results("me", after="6#f")) == []
    assert capsys.readouterr().err == ""


def test_the_packages_ship_the_license_notice_and_type_marker():
    root = Path(__file__).parent.parent
    for package in ("clients/python", "clients/typescript"):
        for name in ("LICENSE", "NOTICE"):
            assert (root / package / name).read_bytes() == (root / name).read_bytes(), package
    assert 'license-files = ["LICENSE", "NOTICE"]' in (root / "clients/python/pyproject.toml").read_text()
    assert (root / "clients/python/src/constellation_space/py.typed").exists()
    npm = json.loads((root / "clients/typescript/package.json").read_text())
    assert npm["license"] == "Apache-2.0" and {"LICENSE", "NOTICE"} <= set(npm["files"])


@pytest.mark.parametrize(
    "url", ["http://localhost:7070", "http://127.0.0.1:7070/", "HTTP://[::1]:7070", " http://LOCALHOST:7070"]
)
def test_every_name_for_this_machine_is_one_origin(url):
    assert results.origin(url) == "http://localhost:7070"
    assert results.origin("https://API.test:443/v1") == "https://api.test"


def test_the_wheel_installs_constellation_space_and_nothing_named_constellation(tmp_path):
    """The import package is constellation_space: `constellation` belongs to another PyPI project,
    and two distributions writing one package directory break each other. It needs Python 3.11
    (the bridges use datetime.UTC), and its metadata says so, so pip refuses an older Python."""
    assert constellation_space.__name__ == "constellation_space"
    root = Path(__file__).parent.parent
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w", str(tmp_path),
         str(root / "clients/python")],
        check=True,
    )
    (wheel,) = tmp_path.glob("constellation_space-*.whl")
    top = {name.split("/")[0] for name in zipfile.ZipFile(wheel).namelist()}
    assert {t for t in top if not t.endswith(".dist-info")} == {"constellation_space"}
    with zipfile.ZipFile(wheel) as zipped:
        (metadata,) = [n for n in zipped.namelist() if n.endswith(".dist-info/METADATA")]
        assert "Requires-Python: >=3.11" in zipped.read(metadata).decode().splitlines()
