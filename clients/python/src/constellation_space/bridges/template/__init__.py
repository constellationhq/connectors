"""A Constellation connector for your own system: subclass Bridge and write apply().

    from constellation_space.bridges.template import Bridge

    class MyBridge(Bridge):
        def apply(self, items, settings, run):
            ...  # write the run's items into your system
            return {"passes": len(items)}

    MyBridge("connector.json").run_forever()

Run it next to your system with CONSTELLATION_API_KEY (a key with predictions:read) set. On start
it checks in with its manifest (connector.json). Constellation lists the connector, only in the
account the key belongs to, as waiting for approval; once someone approves it on Integrations,
workflows can send it their results with the settings the manifest declares. Everything
else (pulling workflow results, skipping item shapes it was not built for, reporting what it wrote,
keeping the cursor so a restart resumes where it stopped, handing approved actions to act()
exactly once, never dying on a bad poll, stopping on a refused key or plan) is the protocol, done
here: https://platform.constellation.space/docs/connectors/custom

Approved actions already carried out are kept beside the cursor (CURSOR_FILE plus ".acted"), so
an approval Constellation sends again, in a later run or after a restart, is never carried out
twice (constellation_space.results.Acted); each is kept a week past its action and past acting on it. An
action past its cancel deadline is never carried out. A bridge with no cursor starts at the newest
run with something for it, never the older history and the decisions in it; BACKFILL=1 applies the
whole history the feed still holds instead. When CONSTELLATION_CONFORMANCE_LOG names a file, each
item applied is appended to it as one JSON line, {"item_id", "state", "acted", "verb"}, for
`constellation conformance` to read.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from constellation_space import results
from constellation_space.client import ApiError, Client
from constellation_space.models import Item, Run


class Bridge:
    def __init__(
        self,
        manifest: str | Path | dict[str, Any] = "connector.json",
        *,
        client: Client | None = None,
        cursor_file: str | Path | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self.manifest = (
            manifest if isinstance(manifest, dict) else json.loads(Path(manifest).read_text())
        )
        self.consumer: str = self.manifest["name"]
        self.client = client or Client()
        # Where the last applied cursor is kept, unless load_cursor/save_cursor keep it elsewhere.
        self.cursor_file = Path(cursor_file or os.environ.get("CURSOR_FILE", "cursor.txt"))
        self.poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else float(os.environ.get("POLL_SECONDS", "15"))
        )
        self.action_log = os.environ.get("CONSTELLATION_CONFORMANCE_LOG")
        self.cursor = self.load_cursor()
        # A first start reads from the feed's newest run, unless BACKFILL=1 asks for its history.
        self._from_head = not self.cursor and os.environ.get("BACKFILL") != "1"
        self._acted = results.Acted(f"{self.cursor_file}.acted")
        self._unreported: dict[str, list[tuple[dict[str, int], list[bool], list[Item]]]] = {}

    def apply(self, items: list[Item], settings: dict[str, Any], run: Run) -> dict[str, int]:
        """Write one run's items into your system; return what you wrote, e.g. {"passes": 12}.

        `items` are this run's results for your connector: entity, target_at, until, value,
        explain, and on a pass entity.track
        (https://platform.constellation.space/docs/connectors/protocol). `settings` are your
        manifest's settings as the workflow set them, every one present. `run` is {workflow,
        workflow_version, run_id, window_from, window_to}: the run is the whole answer for that
        window, so withdraw what you wrote earlier for the same workflow inside the window that
        `items` no longer carries.

        An item may carry an action (a rain-fade swap) in one of four states, read with
        constellation_space.results.action_state(item). "advice": a suggestion, never an instruction.
        "approved": a person approved it in Constellation, an instruction for your system to
        carry out, which act() receives. "pending": held for approval; the action is not sent
        yet. "rejected": the original pass stands. An item in any state is never a plain
        forecast pass.

        Rules (https://platform.constellation.space/docs/connectors/custom): key what you write
        on item["entity"]["id"]; write forecasts where your system keeps proposals, never over
        the customer's own objects, and once an operator has taken one of yours (scheduled it),
        it is theirs: stop updating it.
        Raise when your system is unreachable or failing (5xx): the run is retried. An item your
        system refuses for good (4xx: invalid for it) is skipped and counted, e.g. {"refused": 1},
        so one bad item never stops the results.
        """
        raise NotImplementedError("write the items into your system here")

    def act(self, item: Item, settings: dict[str, Any], run: Run) -> bool:
        """Carry out an approved action (cancel the pass, book the backup); return True once
        done. Called after apply(), once per approval, never for any other state, and never again
        for the same approval (its decision_id; without one, its verb, entity and target) in a
        later run or after a restart. A new approval of the same action is a new decision.
        Raise when your system is failing: the run is retried, without acting again on what was
        done.
        The default carries out nothing."""
        return False

    def load_cursor(self) -> str:
        """The last cursor applied. Keep it in your system if it can hold one, so a moved or
        rebuilt bridge resumes where it stopped. The API it was read from is kept beside it
        (CURSOR_FILE plus ".api"): a cursor and approvals carried out against another API (the
        mock) are never resumed against this one."""
        api = Path(f"{self.cursor_file}.api")
        here = results.origin(self.client.api_url)
        if api.exists() and (there := api.read_text().strip()) != here:
            raise SystemExit(
                f"{self.consumer}: {self.cursor_file} is where this bridge read {there} up to, and"
                f" it now reads {here}. Delete {self.cursor_file}* to start afresh on {here}, or"
                " set CURSOR_FILE to another file"
            )
        return self.cursor_file.read_text().strip() if self.cursor_file.exists() else ""

    def save_cursor(self, cursor: str) -> None:
        Path(f"{self.cursor_file}.api").write_text(results.origin(self.client.api_url))
        self.cursor_file.write_text(cursor)

    def log(self, message: str) -> None:
        print(f"{self.consumer}: {message}", flush=True)

    def check_in(self) -> None:
        self.client.call(
            "POST",
            "/predictions/feed/checkin",
            {
                "consumer": self.consumer,
                "system": "custom",
                "manifest": {
                    k: self.manifest[k]
                    for k in ("name", "title", "description", "settings")
                    if k in self.manifest
                },
                "bridge_version": self.manifest.get("version", "0.1.0"),
                "target": self.manifest.get("title", self.consumer),
            },
        )

    def poll(self) -> None:
        """Applies every run after the cursor, oldest first, moving the cursor past each."""
        if self._from_head:
            self.cursor = results.start_cursor(self.client.call, self.consumer)
            self._from_head = False
            self.log(f"first start: reading after {self.cursor or 'the start'}")
        for entry in self.client.results(self.consumer, self.cursor):
            if self.cursor and entry["cursor"] <= self.cursor:
                # A feed replaying from an older cursor (a failover): applied before, never again.
                self.log(f"skipped {entry['cursor']}: already applied")
                continue
            # What each part wrote, kept until the run is reported: a failed report is retried
            # without writing or acting again.
            done = self._unreported.setdefault(entry["cursor"], [])
            for n, (resource, items) in enumerate(
                results.unpack(entry["otlp"], self.consumer, self.log)
            ):
                if n == len(done):
                    output = json.loads(resource.get("constellation.output") or "{}")
                    settings = (output.get("mapping") or {}).get("settings") or {}
                    run: Run = {
                        "workflow": resource.get("constellation.workflow"),
                        "workflow_version": resource.get("constellation.workflow_version"),
                        "run_id": resource.get("constellation.run_id"),
                        "window_from": resource.get("constellation.window.from"),
                        "window_to": resource.get("constellation.window.to"),
                    }
                    good = results.well_formed(items, run["run_id"], self.log)
                    applied = self.apply(good, settings, run)
                    if malformed := len(items) - len(good):
                        applied = {**applied, "malformed": malformed}
                    items = good
                    acted = [
                        results.action_state(item) == "approved"
                        and self._act(item, settings, run)
                        for item in items
                    ]
                    # So the platform can show that an approval was carried out.
                    if sum(acted):
                        applied = {**applied, "acted": sum(acted)}
                    done.append((applied, acted, items))
                applied, acted, items = done[n]
                results.report(
                    self.client.call,
                    self.consumer,
                    entry["cursor"],
                    applied,
                    self.manifest.get("title", self.consumer),
                )
                self.log(f"applied {applied}, {entry['cursor']}")
                self._record(items, acted)
            del self._unreported[entry["cursor"]]
            # Only a run applied and reported (or not for this connector) moves the cursor: a
            # failed write is retried.
            self.cursor = entry["cursor"]
            self._keep(lambda: self.save_cursor(self.cursor))

    def _act(self, item: Item, settings: dict[str, Any], run: Run) -> bool:
        """Carries out an approved action unless it was carried out before or its cancel deadline
        has passed; says whether it did."""
        if results.past_deadline(item):
            self.log(f"not acting on {item['entity']['id']}: its cancel deadline has passed")
            return False
        if item in self._acted or not self.act(item, settings, run):
            return False
        self._keep(lambda: self._acted.add(item))
        return True

    def _keep(self, save: Callable[[], None]) -> None:
        """Saves the cursor or the approvals carried out; a bridge that cannot would repeat its
        work after every poll or restart, so failing to stops it (exit 1)."""
        try:
            save()
        except OSError as exc:
            print(
                f"{self.consumer}: cannot keep its state, so it would repeat its work: {exc} (make"
                " CURSOR_FILE's folder writable). Stopped; start it again once that is done",
                file=sys.stderr,
            )
            sys.exit(1)

    def _record(self, items: list[Item], acted: list[bool]) -> None:
        if not self.action_log:
            return
        with open(self.action_log, "a") as log:
            for item, did in zip(items, acted):
                line = {
                    "item_id": item["entity"]["id"],
                    "state": results.action_state(item) or "forecast",
                    "acted": did,
                    "verb": (item.get("action") or {}).get("verb"),
                }
                log.write(json.dumps(line) + "\n")

    def run_forever(self) -> None:
        """Checks in (again every results.CHECKIN_S), then polls every poll_seconds. A refused key
        or plan stops it (exit 2) with why; any other failure is logged and retried at the next
        poll. With no key it stops at once (exit 2), unless the API is the mock on this machine."""
        if not self.client.api_key and not results.loopback(self.client.api_url):
            print(
                f"{self.consumer}: CONSTELLATION_API_KEY is not set: make a key with predictions:read"
                f" on {results.site(self.client.api_url)}/keys and set it. Stopped",
                file=sys.stderr,
            )
            sys.exit(2)
        self.log(
            f"{self.client.api_url} as {self.consumer}, from cursor"
            f" {self.cursor or ('the start' if not self._from_head else 'its newest run')}"
        )
        checked_in, next_checkin = False, 0.0
        while True:
            try:
                if time.monotonic() >= next_checkin:
                    self.check_in()
                    next_checkin = time.monotonic() + results.CHECKIN_S
                    if not checked_in:
                        self.log(
                            "checked in; approve it on Integrations if it is new or its"
                            " manifest changed"
                        )
                    checked_in = True
                self.poll()
            except ApiError as exc:
                if why := exc.refusal:
                    print(
                        f"{self.consumer}: {why}. Stopped; start it again once that is done",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                self.log(str(exc))
            except Exception as exc:  # a bad poll or write is logged and retried, never fatal
                print(f"{self.consumer}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(self.poll_seconds)
