"""The CONSTELLATION target's commands, carried out through Constellation's API: APPROVE_SWAP and
DECLINE_SWAP decide a rain-fade swap Constellation holds for a person, as the Platform's Send to
mission control and Decline do (POST /predictions/actions/{run_id}/{item_id}/approve or /reject).
An approved swap comes back as approved advice, in the SWAP packet; Constellation books nothing.

Installed only with the plugin's approvals variable on (plugin.txt), as CONSTELLATION_INT. It
reads nothing: the bridge microservice injects the target's telemetry. COSMOS logs every command
sent, and asks before sending one, as both are HAZARDOUS. A decision Constellation refuses fails
the command with the reason, so Command Sender or the script shows it.

The key is CONSTELLATION_API_KEY in Admin > Secrets, the bridge's, which then needs act:approve;
approving also needs the Operator + Act plan. It is sent only over https, or plain http to this
machine, as the bridge does.
"""

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from openc3.interfaces.interface import Interface  # type: ignore[import-not-found]

DECISIONS = {"APPROVE_SWAP": "approve", "DECLINE_SWAP": "reject"}
KEYS_PAGE = "https://platform.constellation.space/keys"


def loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def key_safe(url: str) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    return scheme == "https" or (scheme == "http" and loopback(url))


def api_key(scope: str) -> str:
    """CONSTELLATION_API_KEY as Admin > Secrets holds it now, else as the interface started."""
    try:
        from openc3.utilities.secrets import Secrets  # type: ignore[import-not-found]

        key = Secrets.get_client().get("CONSTELLATION_API_KEY", scope=scope)
    except Exception:
        key = None
    return (key or os.environ.get("CONSTELLATION_API_KEY") or "").strip()


def refused(status: int, body: bytes, verb: str) -> str:
    """Why Constellation would not take a decision, and what to do, in words."""
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    detail = answer.get("detail", answer) if isinstance(answer, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    error = detail.get("error")
    fix = f"make a live key with predictions:read and act:approve on {KEYS_PAGE}"
    if status == 401:
        return f"Constellation does not accept CONSTELLATION_API_KEY: {fix}"
    if error == "insufficient_scope":
        return f"CONSTELLATION_API_KEY lacks {detail.get('required') or 'act:approve'}: {fix}"
    if error == "live_key_required":
        return f"CONSTELLATION_API_KEY is a test key; deciding takes a live one: {fix}"
    if error == "plan_required":
        return "approving needs the Operator + Act plan; declining is open on every plan"
    if error in ("unknown_action", "unknown_run") or status == 404:
        return "Constellation holds no swap on that item in that run: read CONSTELLATION HELD again"
    if error == "action_decided":
        return "that swap was decided already"
    if error == "action_expired":
        return "its decide-by time has passed, so nothing can be sent for it"
    if error == "action_stale":
        return "a newer run answers it now: read CONSTELLATION HELD again"
    if error:
        return f"Constellation refused to {verb} it ({error})"
    return f"Constellation answered HTTP {status}"


class ConstellationInterface(Interface):
    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.scope = os.environ.get("OPENC3_SCOPE", "DEFAULT")
        # Nothing to read: the bridge injects the CONSTELLATION telemetry.
        self.read_allowed = False
        self._connected = False

    def connection_string(self) -> str:
        return f"Constellation decisions at {self.api_url}"

    def connect(self) -> None:
        if not key_safe(self.api_url):
            raise RuntimeError(
                f"not sending the API key to {self.api_url}: it is not https (plain http only to"
                " localhost)"
            )
        super().connect()
        self._connected = True

    def connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False
        super().disconnect()

    def read_interface(self):  # never called: read_allowed is False
        return None, None

    def write_interface(self, data, extra=None) -> None:
        raise RuntimeError("CONSTELLATION_INT sends decisions, not raw data")

    def write(self, packet) -> None:
        if not self.connected():
            raise RuntimeError(f"Interface not connected for write {self.name}")
        verb = DECISIONS.get(packet.packet_name)
        if verb is None:
            raise RuntimeError(
                f"{packet.packet_name} is not a command CONSTELLATION_INT carries out"
            )
        run_id = str(packet.read("RUN_ID") or "").strip()
        item_id = str(packet.read("ITEM_ID") or "").strip()
        if not run_id or not item_id:
            raise RuntimeError("name the held swap: RUN_ID and ITEM_ID as CONSTELLATION HELD shows")
        self.write_count += 1
        decide(self.api_url, api_key(self.scope), verb, run_id, item_id)


def decide(api_url: str, key: str, verb: str, run_id: str, item_id: str) -> dict:
    """POSTs the decision; returns what Constellation answered, or raises its refusal in words."""
    if not key:
        raise RuntimeError("no CONSTELLATION_API_KEY in Admin > Secrets")
    path = "/".join(urllib.parse.quote(part, safe="") for part in (run_id, item_id))
    req = urllib.request.Request(
        f"{api_url}/predictions/actions/{path}/{verb}",
        data=b"{}",
        method="POST",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(refused(exc.code, exc.read(), verb)) from None
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}
