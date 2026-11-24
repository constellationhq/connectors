"""Typed shapes of workflow results, as the contract's workflow-result.schema.json defines item
schema constellation.item/v2 (tests/test_contract.py holds them to it): an item, its entity,
value and action, and the run a bridge's apply() receives. TypedDicts, so items stay the plain
dicts the API decodes to; nothing is validated at runtime. severity, action.verb, action.mode and
action_status are open strings: a reader handles the known values
(Severity, Verb, Mode, ActionStatus) and does not reject a new one."""

from typing import Any, Literal, TypedDict

# What an item's action is to a bridge (constellation_space.results.action_state); None when it has none.
ActionState = Literal["advice", "approved", "pending", "rejected"]
Severity = Literal["info", "warning", "critical"]
Verb = Literal["reroute", "swap"]
Mode = Literal["suggest", "approve"]
# Only approved may be acted on; any other or unknown status is held.
ActionStatus = Literal["pending", "approved", "rejected"]


class _Entity(TypedDict):
    type: str
    id: str


class Entity(_Entity, total=False):
    link_id: str
    satellite_id: str
    ground_station_id: str
    antenna_id: str
    source: str
    # A pass's ground track, when known: points such as {t, latitude_deg, longitude_deg}.
    track: list[dict[str, Any]]
    # On an antenna a linked ground network maps: state (bookable, booked, unavailable, unknown),
    # the network's offers, the operator's held booking and the networks.
    booking: dict[str, Any]


class Value(TypedDict, total=False):
    p10: float | None
    p50: float | None
    p90: float | None


class SeriesPoint(TypedDict):
    t: str
    value: Any


class _Action(TypedDict):
    verb: str
    mode: str


# Functional form: "from" is a Python keyword.
_ActionFacets = TypedDict(
    "_ActionFacets",
    {
        "from": str,
        "to": str | None,
        "to_antenna_id": str | None,
        "to_at": str | None,
        # A swap is advice. Known values: add (book the backup, keep the original), move (book the
        # backup, release the original); read any other as add.
        "action": str,
        # Why: adds_capacity, overlap, paid_booking or original_empty.
        "reason": str,
        # The backup pass's LOS, length and expected GB (p50).
        "to_until": str,
        "to_duration_s": float,
        "to_gb": float,
        "deadline": str,
        # The GB the advice adds over doing nothing, at the p50.
        "gain_gb": float,
        # Deprecated: use gain_gb. The same gain at the p10.
        "gain_gb_p10": float,
        # The ground network's offer for the backup: link, network, slot_id, price, cancel_deadline_utc.
        "to_offer": dict[str, Any],
        # On a move, the operator's booking of the original to release: link, network,
        # booking_id, price, cancel_deadline_utc, state.
        "from_held": dict[str, Any],
    },
    total=False,
)


class Action(_Action, _ActionFacets):
    pass


class _Item(TypedDict):
    entity: Entity
    target_at: str
    value: Value


class Item(_Item, total=False):
    until: str
    explain: dict[str, dict[str, Any]]
    # A workflow's item carries workflow_version, a lane's model_release_id; never both.
    workflow_version: int
    model_release_id: str
    # What a lane result's value measures (snr_db, rx_power_dbm) and its unit (dB, dBm).
    quantity: str
    unit: str
    series: list[SeriesPoint]
    severity: str
    # What the item's action comes to, as a sentence for a person.
    note: str
    action: Action
    action_status: str
    # The action's own id across runs, and the id of the decision on it (once decided): a
    # bridge acts once per decision_id.
    action_id: str
    decision_id: str


class Run(TypedDict):
    workflow: str | None
    workflow_version: int | None
    run_id: str | None
    window_from: str | None
    window_to: str | None
