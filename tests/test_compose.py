"""The compose files as `docker compose config` reads them: what each requires and what it takes
from the environment (or the .env file beside it)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")


def config(bridge: str, tmp_path: Path, env: dict[str, str], dotenv: str = "") -> tuple[int, str]:
    """`docker compose config` for bridges/<bridge>/deploy/compose.yaml with only `env` set and `dotenv`
    as the .env beside it: (exit code, the config as JSON or the error)."""
    shutil.copy(ROOT / f"bridges/{bridge}/deploy/compose.yaml", tmp_path / "compose.yaml")
    (tmp_path / ".env").write_text(dotenv)
    done = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **env},
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout or done.stderr


def environment(out: str) -> dict[str, str]:
    (service,) = json.loads(out)["services"].values()
    return service["environment"]


YAMCS = {"CONSTELLATION_API_KEY": "k", "YAMCS_URL": "http://y:8090", "YAMCS_INSTANCE": "ops"}


@pytest.mark.parametrize("missing", ["YAMCS_URL", "YAMCS_INSTANCE"])
def test_the_yamcs_compose_requires_where_yamcs_is_and_which_instance(tmp_path, missing):
    code, out = config("yamcs", tmp_path, {k: v for k, v in YAMCS.items() if k != missing})
    assert code != 0 and missing in out
    code, out = config("yamcs", tmp_path, YAMCS)
    assert code == 0
    assert environment(out)["YAMCS_URL"] == "http://y:8090"
    assert environment(out)["YAMCS_INSTANCE"] == "ops"


@pytest.mark.parametrize("bridge, env", [("yamcs", YAMCS), ("lattice", {})])
def test_the_api_url_comes_from_the_environment_or_env_file_with_prod_the_default(
    tmp_path, bridge, env
):
    code, out = config(bridge, tmp_path, env)
    assert code == 0, out
    assert environment(out)["CONSTELLATION_API_URL"] == "https://api.constellation.space"
    dev = "https://dev.api.constellation.space"
    code, out = config(bridge, tmp_path, env, dotenv=f"CONSTELLATION_API_URL={dev}\n")
    assert environment(out)["CONSTELLATION_API_URL"] == dev
    code, out = config(bridge, tmp_path, {**env, "CONSTELLATION_API_URL": dev})
    assert environment(out)["CONSTELLATION_API_URL"] == dev


def filled_env_example(bridge: str) -> str:
    """The bridge's .env.example with every required line given a value, as a user fills it in."""
    lines = (ROOT / f"bridges/{bridge}/deploy/.env.example").read_text().splitlines()
    return "".join(f"{line}x\n" if line.endswith("=") else f"{line}\n" for line in lines)


@pytest.mark.parametrize("bridge", ["yamcs", "lattice"])
def test_the_filled_in_env_file_alone_is_enough_for_every_later_up_or_recreate(tmp_path, bridge):
    code, out = config(bridge, tmp_path, {}, dotenv=filled_env_example(bridge))
    assert code == 0, out
    assert environment(out)["CONSTELLATION_API_KEY"] == "x"


def test_the_yamcs_compose_still_says_so_when_the_env_file_has_no_key(tmp_path):
    dotenv = filled_env_example("yamcs").replace("CONSTELLATION_API_KEY=x", "CONSTELLATION_API_KEY=")
    code, out = config("yamcs", tmp_path, {}, dotenv=dotenv)
    assert code != 0 and "CONSTELLATION_API_KEY" in out
