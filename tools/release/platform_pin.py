"""Point a Platform repository checkout at a connectors release (the release workflow's
platform job runs this, then opens the PR):

    python3 tools/release/platform_pin.py PLATFORM_DIR VERSION CONTRACT_VERSION

It sets CONNECTORS_VERSION and the test that pins it, adds the release's row to the docs'
versions table (copying the newest row's other columns), and extends the item schema row to the
release's minor version, which a release that reads a new item schema must correct by hand. A
second run changes nothing. If the Platform moved one of these, it fails naming the file, rather
than open a PR that misses it.
"""

import re
import sys
from pathlib import Path

PLAIN = re.compile(r"\d+\.\d+\.\d+")
PIN = "src/lib/connectorsVersion.ts"
PIN_TEST = "src/lib/connectorInstall.test.ts"
DOCS = "docs/api/contract-and-versioning.md"
ITEMS = "docs/connectors/index.md"


def sub(root: Path, name: str, pattern: str, repl: str) -> None:
    path = root / name
    text, n = re.subn(pattern, repl, path.read_text(), count=1)
    if n != 1:
        sys.exit(f"{name}: no match for {pattern}; update tools/release/platform_pin.py")
    path.write_text(text)


def bump(root: Path, version: str, contract: str) -> None:
    for v in (version, contract):
        if not PLAIN.fullmatch(v):
            sys.exit(f"not a plain X.Y.Z version: {v!r}")
    sub(root, PIN, r"(export const CONNECTORS_VERSION = ')[^']*'", rf"\g<1>{version}'")
    sub(root, PIN_TEST, r"(expect\(CONNECTORS_VERSION\)\.toBe\(')[^']*'", rf"\g<1>{version}'")
    minor = version.rsplit(".", 1)[0]
    sub(root, ITEMS, r"(\| 0\.3\.x to )\d+\.\d+(\.x \| `constellation\.item/v2`)", rf"\g<1>{minor}\g<2>")
    docs = (root / DOCS).read_text()
    if f"\n| v{version} |" in docs:
        return
    newest = re.search(r"^\| Release \| Contract \|.*\n\| ---.*\n(\| v[^|]+\| [^|]+(\|.*)\n)", docs, re.M)
    if not newest:
        sys.exit(f"{DOCS}: no versions table; update tools/release/platform_pin.py")
    row = f"| v{version} | `contract-v{contract}` {newest.group(2)}\n"
    (root / DOCS).write_text(docs[: newest.start(1)] + row + docs[newest.start(1) :])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    bump(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
