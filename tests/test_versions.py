"""One version across what a release ships: the bridges report it, the Yamcs page carries it, the
Lattice compose file pins the image the release workflow builds for its tag, and the Python and
TypeScript packages and the CLI are it. The release workflow refuses a tag that is not this version."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = r'^VERSION = "(.+)"$'
SOURCES = [
    ("clients/python/src/constellation_space/bridges/yamcs/bridge.py", BRIDGE),
    ("clients/python/src/constellation_space/bridges/lattice/bridge.py", BRIDGE),
    ("bridges/cosmos/microservices/BRIDGE/bridge.py", BRIDGE),
    (
        "bridges/yamcs/widget/resources/META-INF/yamcs/space.constellation.yamcs.ForecastPlugin/"
        "plugin.properties",
        r"^version=(.+)$",
    ),
    ("bridges/lattice/deploy/compose.yaml", r"image: ghcr\.io/constellationhq/lattice-bridge:(.+)$"),
    ("bridges/yamcs/deploy/compose.yaml", r"image: ghcr\.io/constellationhq/yamcs-bridge:(.+)$"),
    ("clients/python/pyproject.toml", r'^version = "(.+)"$'),
    ("clients/typescript/package.json", r'^  "version": "(.+)",$'),
    ("cli/cmd/constellation/main.go", r'^var version = "v(.+)"$'),
]


def test_the_bridges_the_yamcs_page_and_the_lattice_compose_name_one_version():
    found = {}
    for path, pattern in SOURCES:
        match = re.search(pattern, (ROOT / path).read_text(), re.M)
        assert match, f"{path} names no version"
        found[path] = match.group(1)
    assert len(set(found.values())) == 1, found


def test_the_changelog_has_the_section_the_release_notes_are_cut_from():
    (version,) = {
        re.search(pattern, (ROOT / path).read_text(), re.M).group(1) for path, pattern in SOURCES
    }
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## v{re.escape(version)}\b", changelog, re.M)
