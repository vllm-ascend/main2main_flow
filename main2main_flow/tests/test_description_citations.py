"""Regression pins for the PR-description citation fixes (PR #16424):

- the PR table cited upstream commits the adapter never worked on: a
  step's agent-written ``Upstream commit:`` lines are now filtered to
  the step's own commit chunk, and with no surviving citation the row
  falls back to the step's routed ascend-affected commits instead of
  the chunk's end_commit (often a trailing no-op/doc commit)
- ``_make_step`` records ``affected_commits`` (analyzed-unaffected
  commits excluded, unanalyzed kept conservatively)
- monitor/flow fix commits carry a DCO ``Signed-off-by`` trailer via
  ``git commit -s`` (an unsigned fix commit failed the upstream DCO
  check and blocked the PR it was meant to unblock)
"""

import inspect
import re

from main2main_flow import flow as flow_mod
from main2main_flow.scripts.utils.plan_steps import _make_step

LINK = "https://github.com/vllm-project/vllm/commit/{}"


def test_filter_upstream_links_drops_out_of_chunk():
    chunk = {"a" * 8, "b" * 8}
    links = [
        f"[{'a' * 8}]({LINK.format('a' * 40)})",   # in chunk -> kept
        f"[{'c' * 8}]({LINK.format('c' * 40)})",   # outside -> dropped
        f"[{'d' * 40}]({LINK.format('d' * 40)})",  # full-sha, prefix outside -> dropped
        f"[vLLM PR #54853]({LINK.format('e' * 40)})",  # non-sha label -> kept
        f"[{'b' * 7}]({LINK.format('b' * 40)})",   # 7-char hex prefix in chunk -> kept
    ]
    kept = flow_mod._filter_upstream_links(links, chunk)
    assert kept == [links[0], links[3], links[4]]


def test_filter_upstream_links_no_shas_kept_as_is():
    links = ["[vLLM PR #1](https://github.com/vllm-project/vllm/pull/1)"]
    assert flow_mod._filter_upstream_links(links, set()) == links


def _commit(sha: str) -> dict[str, str]:
    return {"sha": sha, "subject": f"subject {sha[:7]}"}


def test_make_step_records_affected_commits():
    impacts = {
        "a" * 40: {"ascend_affected": True},
        "b" * 40: {"ascend_affected": False},  # analyzed-unaffected
        # c unanalyzed -> conservative affected
    }
    step = _make_step(
        1, [_commit("a" * 40), _commit("b" * 40), _commit("c" * 40)],
        "0" * 40, 100, 100, True, impacts)
    assert step["affected_commits"] == ["a" * 40, "c" * 40]
    assert step["end_commit"] == "c" * 40


def test_make_step_without_impacts_all_affected():
    step = _make_step(1, [_commit("a" * 40), _commit("b" * 40)],
                      "0" * 40, 10, 10, True, None)
    assert step["affected_commits"] == ["a" * 40, "b" * 40]


def test_fix_and_squash_commits_are_signed():
    # DCO: every commit pushed to upstream must carry Signed-off-by —
    # the monitor's fix commit and the push-time force-squash both go
    # through `git commit -s`.
    from main2main_flow.scripts.utils import pr_ci_monitor, push_to_github
    mon_src = inspect.getsource(pr_ci_monitor)
    assert re.search(r'"commit",\s*"-s",\s*"-m"', mon_src)
    push_src = inspect.getsource(push_to_github)
    assert re.search(r'"commit",\s*"-s",\s*"-m",\s*'
                     r'f"main2main: sync vllm upstream', push_src)
