"""The API contract, committed unpacked under contract/ and pinned file by file in contract.lock.

    python3 tools/contract/fetch.py                  # make contract: check contract/ against the lock
    python3 tools/contract/fetch.py --update 1.5.0   # make contract-update VERSION=1.5.0
    python3 tools/contract/fetch.py --diff OLD NEW   # what changed between two locks or folders

contract/ holds the files of the tarball the API's repository publishes (OpenAPI, workflow result
schema, telemetry proto, MCP tools, ...), and contract.lock pins its version, the published
tarball's sha256 and each file's sha256. make contract checks every file against the lock, with no
network and no token: a file changed, added or missing fails it. --update downloads the release
asset contract-v<version>/contract-<version>.tar.gz with gh from CONTRACT_REPO (the API's
repository, which needs read access), replaces contract/ with its files, rewrites contract.lock
and prints what changed. --diff prints the same between two contracts, each a lock file (the
contract/ folder beside it) or a folder: endpoints, fields, enum values, MCP tools and the
telemetry proto, as Markdown (the lock-bump PR comment). CONTRACT_DIR=<dir> make contract copies
a local, unpublished contract into contract/ for development instead; nothing is checked.
"""

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "contract.lock"
CONTRACT = ROOT / "contract"


def download(version: str, into: Path) -> Path:
    repo = os.environ.get("CONTRACT_REPO", "")
    if not repo:
        sys.exit("set CONTRACT_REPO to the API's repository (owner/name) to download a contract")
    tag, name = f"contract-v{version}", f"contract-{version}.tar.gz"
    done = subprocess.run(
        ["gh", "release", "download", tag, "-R", repo, "-p", name, "-D", str(into)],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        sys.exit(f"could not download {tag} from {repo}: {done.stderr.strip()}")
    return into / name


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(folder: Path) -> dict[str, str]:
    """Each file under `folder`, by its path there, and its sha256."""
    return {
        f.relative_to(folder).as_posix(): sha256(f)
        for f in sorted(folder.rglob("*"))
        if f.is_file()
    }


def verify(lock: dict, folder: Path = CONTRACT) -> list[str]:
    """How `folder` differs from what `lock` pins; empty when every file matches."""
    pinned, found = lock.get("files", {}), files(folder) if folder.is_dir() else {}
    return (
        [f"{name} is missing" for name in sorted(set(pinned) - set(found))]
        + [f"{name} is not in contract.lock" for name in sorted(set(found) - set(pinned))]
        + [
            f"{name} has sha256 {found[name]}, contract.lock pins {pinned[name]}"
            for name in sorted(set(pinned) & set(found))
            if found[name] != pinned[name]
        ]
    )


def unpack(tarball: Path, out: Path) -> None:
    """Replaces `out` with the files in `tarball`; a tarball holding one folder is unpacked from
    inside it."""
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(tarball) as tar:
        tar.extractall(tmp, filter="data")
        entries = list(Path(tmp).iterdir())
        top = entries[0] if len(entries) == 1 and entries[0].is_dir() else Path(tmp)
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(top, out)


def read(folder: Path, name: str) -> Any:
    """A contract file: JSON parsed, the proto as text; empty when the contract has none."""
    path = folder / name
    if not path.exists():
        return "" if name.endswith(".proto") else {}
    return path.read_text() if name.endswith(".proto") else json.loads(path.read_text())


def schemas(folder: Path) -> dict[str, dict]:
    """Every object the contract defines, by name: the OpenAPI components, the workflow result
    ("result") and its parts ("result.Action")."""
    result = read(folder, "workflow-result.schema.json")
    found = dict(read(folder, "openapi.json").get("components", {}).get("schemas", {}))
    found.update({f"result.{k}": v for k, v in result.pop("$defs", {}).items()})
    return found | ({"result": result} if result else {})


def fields(objects: dict[str, dict]) -> dict[str, dict]:
    """Each object's field, "Object.field": its shape (the definition without prose or enum
    values), whether it is required, and its enum values."""

    def shape(node: Any, names: bool = False) -> Any:
        """`names`: node maps property names (which are kept whatever they are) to schemas."""
        if isinstance(node, dict):
            prose = () if names else ("description", "title", "enum")
            return {
                k: shape(v, not names and k == "properties")
                for k, v in node.items()
                if k not in prose
            }
        return [shape(v) for v in node] if isinstance(node, list) else node

    return {
        f"{name}.{field}": {
            "shape": shape(spec),
            "required": field in obj.get("required", []),
            "enum": enums(spec),
        }
        for name, obj in objects.items()
        for field, spec in obj.get("properties", {}).items()
    }


def enums(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(map(str, node.get("enum", []))).union(*map(enums, node.values()))
    return set().union(*map(enums, node)) if isinstance(node, list) else set()


def endpoints(folder: Path) -> set[str]:
    paths = read(folder, "openapi.json").get("paths", {})
    return {f"{m.upper()} {p}" for p, ops in paths.items() for m in ops if m != "parameters"}


def added_removed(old: Any, new: Any, what: str) -> list[str]:
    return [f"- added {what} `{k}`" for k in sorted(set(new) - set(old))] + [
        f"- removed {what} `{k}`" for k in sorted(set(old) - set(new))
    ]


def field_changes(old: dict[str, dict], new: dict[str, dict], what: str) -> list[str]:
    lines = added_removed(old, new, what)
    for k in sorted(set(old) & set(new)):
        if old[k]["required"] != new[k]["required"]:
            lines.append(f"- `{k}` is now {'required' if new[k]['required'] else 'optional'}")
        if old[k]["shape"] != new[k]["shape"]:
            before, after = (json.dumps(x[k]["shape"], sort_keys=True) for x in (old, new))
            lines.append(f"- changed `{k}` from `{before}` to `{after}`")
    return lines


def enum_changes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    lines = []
    for k in sorted(set(old) & set(new)):
        added, removed = new[k]["enum"] - old[k]["enum"], old[k]["enum"] - new[k]["enum"]
        said = [
            f"{verb} {', '.join(f'`{v}`' for v in sorted(values))}"
            for verb, values in (("added", added), ("removed", removed))
            if values
        ]
        if said:
            lines.append(f"- `{k}`: {'; '.join(said)}")
    return lines


def diff(old: Path | None, new: Path | None) -> str:
    """What changed from contract `old` to contract `new` (folders; None for none), as Markdown."""
    version = [(f / "VERSION").read_text().strip() if f else "none" for f in (old, new)]
    title = f"### API contract {version[0]} to {version[1]}"
    if not (old and new):
        return f"{title}\n\nNothing to compare: one side pins no published contract.\n"
    old_fields, new_fields = fields(schemas(old)), fields(schemas(new))
    old_tools, new_tools = read(old, "mcp-tools.json"), read(new, "mcp-tools.json")
    old_inputs, new_inputs = (
        fields({name: t.get("inputSchema", {}) for name, t in tools.items()})
        for tools in (old_tools, new_tools)
    )
    proto = "\n".join(
        difflib.unified_diff(
            read(old, "telemetry.proto").splitlines(),
            read(new, "telemetry.proto").splitlines(),
            "old/telemetry.proto",
            "new/telemetry.proto",
            lineterm="",
        )
    )
    sections = {
        "Endpoints": added_removed(endpoints(old), endpoints(new), "endpoint"),
        "Fields": field_changes(old_fields, new_fields, "field"),
        "Enum values": enum_changes(old_fields, new_fields) + enum_changes(old_inputs, new_inputs),
        "MCP tools": added_removed(old_tools, new_tools, "tool")
        + field_changes(old_inputs, new_inputs, "input"),
        "Telemetry proto": [f"```diff\n{proto}\n```"] if proto else [],
    }
    out = [title, ""]
    for title, lines in sections.items():
        if lines:
            out += [f"#### {title}", "", *lines, ""]
    if not any(sections.values()):
        out.append("No change to endpoints, fields, enum values, MCP tools or the proto.")
    return "\n".join(out).rstrip() + "\n"


def contract(source: str) -> Path | None:
    """A --diff side: a lock file (the contract/ folder beside it) or a contract folder; None when
    the lock pins no contract yet."""
    path = Path(source)
    if path.is_dir():
        return path
    lock = json.loads(path.read_text() or "{}")
    return path.parent / "contract" if lock.get("files") else None


def main(argv: list[str]) -> None:
    if argv[:1] == ["--diff"] and len(argv) == 3:
        print(diff(*(contract(source) for source in argv[1:])), end="")
        return
    if local := os.environ.get("CONTRACT_DIR"):
        shutil.rmtree(CONTRACT, ignore_errors=True)
        shutil.copytree(local, CONTRACT)
        print(f"contract/ is {local} (CONTRACT_DIR), not contract.lock's", file=sys.stderr)
        return
    if argv[:1] == ["--update"] and len(argv) == 2:
        version = argv[1]
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before"
            if CONTRACT.is_dir():
                shutil.copytree(CONTRACT, before)
            tarball = download(version, Path(tmp))
            unpack(tarball, CONTRACT)
            lock = {"version": version, "tarball_sha256": sha256(tarball), "files": files(CONTRACT)}
            LOCK.write_text(json.dumps(lock, indent=2) + "\n")
            print(diff(before if before.is_dir() else None, CONTRACT), end="")
        print(f"contract/ is contract {version}", file=sys.stderr)
        return
    if argv:
        sys.exit(__doc__)
    lock = json.loads(LOCK.read_text())
    if problems := verify(lock):
        sys.exit(
            "contract/ is not the contract contract.lock pins:\n  "
            + "\n  ".join(problems)
            + "\nRun make contract-update VERSION=<version> rather than editing contract/."
        )
    print(f"contract/ is contract {lock['version']}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
