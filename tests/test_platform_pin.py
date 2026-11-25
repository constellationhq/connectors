"""tools/release/platform_pin.py points a Platform repository checkout at a new connectors
release: the pin, its test and a row of the docs' versions table, copied from the newest row."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("platform_pin", ROOT / "tools" / "release" / "platform_pin.py")
platform_pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_pin)

ROW = "| 3.11 or newer | 18 or newer for the client | 1.22 or newer |"
DOCS = f"""# Contract and versioning

| Release | Contract | Python | Node | Go |
| --- | --- | --- | --- | --- |
| v0.8.3 | `contract-v2.1.0` {ROW}
| v0.8.2 | `contract-v2.1.0` {ROW}

After the table.
"""

ITEMS = "| Connectors | Item schema |\n| --- | --- |\n| 0.3.x to 0.8.x | `constellation.item/v2` |\n"


@pytest.fixture
def platform(tmp_path: Path) -> Path:
    (tmp_path / "src/lib").mkdir(parents=True)
    (tmp_path / "docs/api").mkdir(parents=True)
    (tmp_path / "src/lib/connectorsVersion.ts").write_text("/** The pin. */\nexport const CONNECTORS_VERSION = '0.8.3'\n")
    (tmp_path / "src/lib/connectorInstall.test.ts").write_text(
        "  it('pins', () => {\n    expect(CONNECTORS_VERSION).toBe('0.8.3')\n  })\n"
    )
    (tmp_path / "docs/api/contract-and-versioning.md").write_text(DOCS)
    (tmp_path / "docs/connectors").mkdir()
    (tmp_path / "docs/connectors/index.md").write_text(ITEMS)
    return tmp_path


def test_a_release_moves_the_pin_its_test_and_adds_its_row(platform: Path):
    platform_pin.bump(platform, "0.9.0", "2.2.0")
    assert "export const CONNECTORS_VERSION = '0.9.0'\n" in (platform / "src/lib/connectorsVersion.ts").read_text()
    assert "expect(CONNECTORS_VERSION).toBe('0.9.0')" in (platform / "src/lib/connectorInstall.test.ts").read_text()
    docs = (platform / "docs/api/contract-and-versioning.md").read_text()
    assert docs == DOCS.replace(
        "| v0.8.3 |", f"| v0.9.0 | `contract-v2.2.0` {ROW}\n| v0.8.3 |", 1
    )


def test_the_item_schema_row_reaches_the_new_minor(platform: Path):
    platform_pin.bump(platform, "0.9.0", "2.2.0")
    assert "| 0.3.x to 0.9.x | `constellation.item/v2` |" in (platform / "docs/connectors/index.md").read_text()


def test_running_it_twice_adds_one_row(platform: Path):
    platform_pin.bump(platform, "0.9.0", "2.2.0")
    platform_pin.bump(platform, "0.9.0", "2.2.0")
    assert (platform / "docs/api/contract-and-versioning.md").read_text().count("| v0.9.0 |") == 1


def test_a_platform_whose_files_moved_fails_loudly(platform: Path):
    (platform / "src/lib/connectorsVersion.ts").write_text("export const VERSION = '0.8.3'\n")
    with pytest.raises(SystemExit, match="connectorsVersion.ts"):
        platform_pin.bump(platform, "0.9.0", "2.2.0")


def test_a_docs_page_without_the_table_fails_loudly(platform: Path):
    (platform / "docs/api/contract-and-versioning.md").write_text("# No table\n")
    with pytest.raises(SystemExit, match="contract-and-versioning.md"):
        platform_pin.bump(platform, "0.9.0", "2.2.0")


@pytest.mark.parametrize("version", ["v0.9.0", "0.9", "0.9.0-rc.1", ""])
def test_only_a_plain_release_version_is_accepted(platform: Path, version: str):
    with pytest.raises(SystemExit):
        platform_pin.bump(platform, version, "2.2.0")
