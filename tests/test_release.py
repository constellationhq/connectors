"""The release workflow publishes only from constellationhq/connectors (never a fork), only after
every platform install passes, and only once SHA256SUMS is signed keyless and the released CLI
verifies it, so nothing ships unsigned."""

import json
import re
import subprocess
import tarfile
from pathlib import Path

RELEASE = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text()
PUBLIC = "github.repository == 'constellationhq/connectors'"


def job(name: str) -> str:
    """The text of one job, up to the next top-level job."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z-]+:\n|^  # |\Z)", RELEASE, re.M | re.S)
    assert match, f"no {name} job"
    return match.group(1)


def test_only_constellationhq_connectors_publishes_never_a_fork():
    for name in ("images", "release", "pypi", "npm", "rubygems", "maven-central", "live"):
        assert re.search(rf"^    if: {re.escape(PUBLIC)}", job(name), re.M), name
    for name in ("artifacts", "install"):
        assert "write" not in job(name).split("steps:")[0], name


def test_the_release_is_signed_keyless_before_anything_is_released():
    release = job("release")
    assert "continue-on-error" not in RELEASE
    assert "--key" not in RELEASE and "tlog-upload=false" not in RELEASE
    assert not re.search(r"if: .*(sign|SIGN)", release)
    sign = release.index("cosign sign-blob --yes --bundle SHA256SUMS.sigstore.json SHA256SUMS")
    assert sign < release.index("verify .") < release.index("gh release create")
    assert "cosign sign --yes" in job("images")


def test_the_registries_publish_from_this_repositorys_identity():
    pypi, npm = job("pypi"), job("npm")
    assert "id-token: write" in pypi and "password" not in pypi and "environment: pypi" in pypi
    assert "--provenance" in npm and "id-token: write" in npm and "NPM_TOKEN" in npm
    rubygems = job("rubygems")
    assert "id-token: write" in rubygems and "trusted_publisher/exchange_token" in rubygems
    assert "vars.RUBYGEMS_OWNER_ID" in rubygems
    assert "RUBYGEMS_API_KEY" not in RELEASE
    maven = job("maven-central")
    for secret in ("MAVEN_CENTRAL_USERNAME", "MAVEN_CENTRAL_PASSWORD", "MAVEN_GPG_PRIVATE_KEY"):
        assert secret in maven
    assert "<scm><url>https://github.com/constellationhq/connectors</url></scm>" in maven


def test_nothing_publishes_until_every_install_passes():
    assert re.search(r"^    needs: \[artifacts, install\]$", job("images"), re.M)
    assert re.search(r"^    needs: \[artifacts, images\]$", job("release"), re.M)
    assert "uses: ./.github/workflows/install.yml" in job("install")


def test_the_install_matrix_covers_every_yamcs_the_readme_lists():
    root = Path(__file__).resolve().parents[1]
    install = (root / ".github/workflows/install.yml").read_text()
    tested = re.search(r"^        yamcs: \[(.*)\]$", install, re.M)
    rows = [r for r in (root / "README.md").read_text().splitlines() if r.startswith("| v")]
    assert tested and rows
    listed = rows[-1].split("|")[4]
    assert re.findall(r"[\d.]+", tested.group(1)) == re.findall(r"\d+\.\d+\.\d+", listed)
    openmct = re.search(r"OPENMCT_VERSION: (\S+)", install)
    assert openmct and rows[-1].split("|")[8].strip() == openmct.group(1)


REGISTRIES = ("pypi", "npm", "rubygems", "maven-central")


def test_a_live_release_never_names_a_package_the_registries_lack():
    """The scaffolds the released CLI makes pin this version's packages: the release stays a
    draft until every registry has them, and fails before building while they cannot publish."""
    gate = job("artifacts").split("steps:")[1].split("- uses:")[0]
    assert "vars.PUBLIC_REGISTRIES != 'true'" in gate and "exit 1" in gate
    assert "gh release create \"$TAG\" --draft" in job("release")
    assert "--draft=false" not in job("release")
    for name in REGISTRIES:
        assert re.search(r"^    needs: release$", job(name), re.M), name
        assert "PUBLIC_REGISTRIES" not in job(name), name
    live = job("live")
    assert re.search(rf"^    needs: \[release, {', '.join(REGISTRIES)}\]$", live, re.M)
    assert "gh release edit \"$TAG\" --draft=false" in live
    assert "continue-on-error" not in live and "always()" not in RELEASE
    assert live.index("--draft=false") < live.index("aws s3 cp")


def test_the_packed_npm_client_has_no_prepare_script(tmp_path):
    """npm runs (and npm 11 warns about) a dependency's prepare script on a tarball install; the
    published client builds in CI and the release, never on a user's machine."""
    client = Path(__file__).resolve().parents[1] / "clients/typescript"
    subprocess.run(
        ["npm", "pack", "--silent", "--ignore-scripts", "--pack-destination", str(tmp_path)],
        cwd=client,
        check=True,
        capture_output=True,
    )
    (tarball,) = tmp_path.glob("*.tgz")
    with tarfile.open(tarball) as packed:
        manifest = json.load(packed.extractfile("package/package.json"))  # type: ignore[arg-type]
    assert "prepare" not in manifest["scripts"] and "build" in manifest["scripts"]


def test_maven_central_waits_until_central_has_published():
    """Central validates after the upload returns: the job fails, leaving the release a draft,
    unless the deployment reaches PUBLISHED."""
    maven = job("maven-central")
    assert 'id="$(curl' in maven and "publisher/status?id=$id" in maven
    assert "PUBLISHED) exit 0" in maven and "FAILED)" in maven
    assert maven.rstrip().endswith("exit 1")


def test_the_release_goes_live_only_once_anyone_can_pull_the_images():
    """ghcr makes a new package private; the compose files, docker run and cosign verify in a
    live release would fail for everyone outside the organization."""
    live = job("live")
    check = live.index('ghcr.io/token?scope=repository:$image:pull"')
    assert 'image="constellationhq/$b-bridge"' in live
    assert check < live.index("--draft=false")
    assert "for b in yamcs lattice" in live and "Change visibility" in live


def test_the_release_notes_are_the_changelogs_section_for_the_tag():
    """--generate-notes lists this repository's pull requests, which hold none of the history."""
    release = job("release")
    assert "--generate-notes" not in RELEASE
    assert "contents/CHANGELOG.md?ref=$TAG" in release and "--notes-file notes.md" in release
