"""The Python models against the contract contract.lock pins (contract/), and how
tools/contract/fetch.py unpacks a published contract and checks contract/ against the lock."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from typing import get_args

import pytest
from constellation_space import models

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "workflow-result.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA.read_text())


def keys(typed) -> tuple[set[str], set[str]]:
    return set(typed.__required_keys__ | typed.__optional_keys__), set(typed.__required_keys__)


def field(schema: dict, node: dict, name: str) -> dict:
    """Property `name` of `node`, its $ref (a model the schema defines once) followed."""
    prop = node["properties"][name]
    return schema["$defs"][prop["$ref"].rsplit("/", 1)[1]] if "$ref" in prop else prop


def known(prop: dict) -> set[str]:
    return {v for option in prop["anyOf"] if "enum" in option for v in option["enum"]}


@pytest.mark.parametrize(
    "typed, path",
    [
        (models.Item, ()),
        (models.Entity, ("entity",)),
        (models.Value, ("value",)),
        (models.Action, ("action",)),
    ],
)
def test_each_model_has_the_fields_the_schema_names(schema, typed, path):
    node = schema
    for name in path:
        node = field(schema, node, name)
    assert keys(typed) == (set(node["properties"]), set(node.get("required", [])))


def test_the_known_values_are_the_schemas(schema):
    props, action = schema["properties"], field(schema, schema, "action")["properties"]
    assert set(get_args(models.Severity)) == known(props["severity"])
    assert set(get_args(models.ActionStatus)) == known(props["action_status"])
    assert set(get_args(models.Verb)) == known(action["verb"])
    assert set(get_args(models.Mode)) == known(action["mode"])


def load_fetch():
    spec = importlib.util.spec_from_file_location("fetch", ROOT / "tools" / "contract" / "fetch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tarball(path: Path, files: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("prefix", ["", "contract-1.0.0/"])
def test_a_published_contract_unpacks_flat_whether_or_not_it_is_in_a_folder(tmp_path, prefix):
    fetch, tgz, out = load_fetch(), tmp_path / "c.tar.gz", tmp_path / "out"
    tarball(tgz, {f"{prefix}VERSION": b"1.0.0\n", f"{prefix}openapi.json": b"{}"})
    (out / "stale").mkdir(parents=True)
    fetch.unpack(tgz, out)
    assert sorted(p.name for p in out.iterdir()) == ["VERSION", "openapi.json"]


def test_a_changed_added_or_missing_file_fails_the_lock(tmp_path):
    fetch, folder = load_fetch(), tmp_path / "contract"
    folder.mkdir()
    (folder / "VERSION").write_text("1.0.0\n")
    (folder / "openapi.json").write_text("{}")
    lock = {"version": "1.0.0", "files": fetch.files(folder)}
    assert fetch.verify(lock, folder) == []
    (folder / "openapi.json").write_text('{"edited": true}')
    (folder / "extra.json").write_text("{}")
    (folder / "VERSION").unlink()
    problems = fetch.verify(lock, folder)
    assert problems[0] == "VERSION is missing"
    assert problems[1] == "extra.json is not in contract.lock"
    assert problems[2].startswith("openapi.json has sha256 ")
    assert fetch.verify(lock, tmp_path / "absent") == ["VERSION is missing", "openapi.json is missing"]


def contract_dir(path: Path, version: str, result: dict, openapi: dict, tools: dict, proto: str):
    path.mkdir()
    (path / "VERSION").write_text(version + "\n")
    (path / "workflow-result.schema.json").write_text(json.dumps(result))
    (path / "openapi.json").write_text(json.dumps(openapi))
    (path / "mcp-tools.json").write_text(json.dumps(tools))
    (path / "telemetry.proto").write_text(proto)
    return path


def test_a_lock_bump_says_what_changed_in_endpoints_fields_enums_tools_and_proto(tmp_path):
    fetch = load_fetch()
    verb = {"anyOf": [{"enum": ["swap"], "type": "string"}, {"type": "string"}], "title": "Verb"}
    result = {
        "properties": {"action": {"$ref": "#/$defs/Action"}, "until": {"type": "string"}},
        "$defs": {"Action": {"properties": {"verb": verb}, "required": ["verb"]}},
    }
    openapi = {
        "paths": {"/a": {"get": {}, "parameters": []}, "/gone": {"post": {}}},
        "components": {"schemas": {"CheckIn": {"properties": {"title": {"type": "string"}}}}},
    }
    tools = {"approve_action": {"inputSchema": {"properties": {"run_id": {"type": "string"}}}}}
    old = contract_dir(tmp_path / "old", "1.0.0", result, openapi, tools, "message A {\n}\n")
    new_verb = {**verb, "anyOf": [{"enum": ["swap", "cancel"], "type": "string"}, verb["anyOf"][1]]}
    result["properties"]["decision_id"] = {"type": "string", "description": "new prose"}
    result["properties"]["until"] = {"type": "integer"}
    result["$defs"]["Action"]["properties"]["verb"] = new_verb
    openapi["paths"] = {"/a": {"get": {}, "put": {}}}
    openapi["components"]["schemas"]["CheckIn"]["required"] = ["title"]
    tools = {
        "approve_action": {"inputSchema": {"properties": {"item_id": {"type": "string"}}}},
        "list_actions": {},
    }
    new = contract_dir(
        tmp_path / "new", "1.1.0", result, openapi, tools, "message A {\n  int64 t = 1;\n}\n"
    )

    said = fetch.diff(old, new)

    assert said.startswith("### API contract 1.0.0 to 1.1.0\n")
    for line in [
        "- added endpoint `PUT /a`",
        "- removed endpoint `POST /gone`",
        "- added field `result.decision_id`",
        '- changed `result.until` from `{"type": "string"}` to `{"type": "integer"}`',
        "- `CheckIn.title` is now required",
        "- `result.Action.verb`: added `cancel`",
        "- added tool `list_actions`",
        "- added input `approve_action.item_id`",
        "- removed input `approve_action.run_id`",
        "+  int64 t = 1;",
    ]:
        assert line in said.splitlines(), line
    assert "result.Action.verb` from" not in said
    assert fetch.diff(new, new).endswith(
        "No change to endpoints, fields, enum values, MCP tools or the proto.\n"
    )
    assert "Nothing to compare" in fetch.diff(None, new)


def test_a_lock_that_pins_nothing_yet_has_no_contract_to_compare(tmp_path):
    fetch = load_fetch()
    lock = tmp_path / "base.lock"
    lock.write_text('{"version": "1.0.0", "file": "contract/contract-1.0.0.tar.gz", "sha256": ""}')
    assert fetch.contract(str(lock)) is None
    assert fetch.contract(str(tmp_path)) == tmp_path


def test_contract_is_the_one_contract_lock_pins():
    fetch = load_fetch()
    lock = json.loads((ROOT / "contract.lock").read_text())
    assert fetch.verify(lock) == []
    assert (ROOT / "contract" / "VERSION").read_text().strip() == lock["version"]
    assert fetch.contract(str(ROOT / "contract.lock")) == ROOT / "contract"
