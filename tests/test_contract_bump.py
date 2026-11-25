"""The contract-bump workflow merges its PR itself, with a merge commit, only once every check on
the head it merges has passed: main requires no status checks, so GitHub auto-merge would not wait."""

import re
from pathlib import Path

BUMP = (Path(__file__).resolve().parents[1] / ".github/workflows/contract-bump.yml").read_text()


def step(name: str) -> str:
    match = re.search(rf"^      - name: {name}\n(.*?)(?=^      - |\Z)", BUMP, re.M | re.S)
    assert match, f"no step {name}"
    return match.group(1)


def test_the_bump_pr_merges_only_after_its_checks_pass_on_the_head_they_ran_on():
    merge = step("Merge the bump PR once its checks pass")
    assert merge.index('if gh pr checks "$number"') < merge.index("gh pr merge")
    assert 'gh pr merge "$number" --merge --match-head-commit "$head"' in merge
    assert not re.search(r"--(auto|squash|rebase|admin)\b", BUMP)


def test_a_merged_bump_is_not_reopened_in_the_same_run():
    assert "if: steps.newest.outputs.version && !steps.merge.outputs.merged" in step("Open or update the bump PR")
