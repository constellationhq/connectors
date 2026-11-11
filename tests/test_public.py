"""This repository is public: no tracked file names a private GitHub organization or its accounts, an internal host, or a credential."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = re.compile(
    rb"constellationos|_conspace"  # the private organization and its managed accounts
    rb"|[a-z0-9-]+\.internal\b|amazonaws\.com|arn:aws:"  # internal hosts, AWS resources
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}"
    rb"|github_pat_|xox[abprs]-",
    re.I,
)
# The API's own OpenAPI title, as api.constellation.space serves it: the product name, not the
# organization. The API's repository renames it in a later contract.
API_TITLE = b'"title": "ConstellationOS"'
SELF = Path(__file__).relative_to(ROOT).as_posix()


def tracked() -> dict[str, bytes]:
    names = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.split(b"\0")
    files = {
        n.decode(): (ROOT / n.decode()).read_bytes() for n in names if n and n.decode() != SELF
    }
    return {
        n: body.replace(API_TITLE, b"") if n == "contract/openapi.json" else body
        for n, body in files.items()
    }


def test_no_file_names_anything_private():
    named = {
        n: PRIVATE.search(body).group().decode()
        for n, body in tracked().items()
        if PRIVATE.search(body)
    }
    assert named == {}
