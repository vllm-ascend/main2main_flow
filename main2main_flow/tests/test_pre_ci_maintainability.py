"""pre_ci maintainability ADVISORIES: rejected shapes are visible mechanically.

PR vllm-ascend#20156 (rejected) made every difference look green while adding
a compat-only helper + state, a `find_spec` capability probe and a
`draft_split[:3]` arity slice.  `_check_maintainability` cannot judge whether a
helper is justified, so it never blocks (`violations` is always empty): it
reports the syntactically visible rejected shapes so the job log and
pre_ci_check.json point at them, and adapter-qa / the gate review decide.
"""
from main2main_flow.scripts.utils.pre_ci_check import _check_maintainability


def _lines(file: str, *texts: str) -> list[dict[str, str]]:
    return [{"file": file, "line_no": str(i + 1), "text": t}
            for i, t in enumerate(texts)]


def _rules(result: dict) -> list[str]:
    return [a["rule"] for a in result["advisories"]]


def test_new_def_without_guard_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/ec_manager/score_ec_manager.py",
        "    def _add_cached_input_id(self, request, input_id):",
        "        return None",
    ))
    assert result["violations"] == []
    assert _rules(result) == ["new_def"]
    assert result["advisories"][0]["file"].endswith("score_ec_manager.py")


def test_new_def_in_guarded_file_is_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/worker/v2/model_runner.py",
        "        if vllm_version_is(\"0.30.0\"):",
        "            def _helper(self):",
        "                ...",
    ))
    assert result["advisories"] == []


def test_new_self_state_without_guard_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/ec_manager/score_ec_manager.py",
        "        self.request_hash_refs: dict = {}",
    ))
    assert _rules(result) == ["new_state"]


def test_new_self_state_in_guarded_file_is_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/patch/worker/patch_distributed.py",
        "        if vllm_version_is(\"0.30.0\"):",
        "            self.cpu_timeout = get_cpu_distributed_timeout_or_none()",
    ))
    assert result["advisories"] == []


def test_capability_probe_is_always_advisory() -> None:
    probe = _check_maintainability(_lines(
        "vllm_ascend/models/minimax_m3/msa_m3.py",
        "from importlib.util import find_spec",
        "_CANN_INDEX_SCORE_AVAILABLE = find_spec(\"cann\") is not None",
    ))
    assert _rules(probe) == ["capability_probe"]

    hasattr_probe = _check_maintainability(_lines(
        "vllm_ascend/x.py",
        "        if hasattr(obj, \"cpu_timeout\"):",
        "            ...",
    ))
    assert _rules(hasattr_probe) == ["capability_probe"]


def test_tuple_unpack_slice_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/worker/v2/model_runner.py",
        "        cu_num_logits, query_start_loc, total = draft_split[:3]",
    ))
    assert _rules(result) == ["arity_slice"]


def test_plain_slice_and_indexing_are_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/x.py",
        "        first = tokens[:3]",
        "        third = tokens[3]",
        "        total = tokens[0] + tokens[1]",
    ))
    assert result["advisories"] == []


def test_non_python_files_and_comments_are_ignored() -> None:
    result = _check_maintainability(_lines(
        "docs/note.md",
        "def not_python():",
        "vllm_ascend/x.py",
        "    # self.cached = {}  (old comment)",
        "    # find_spec('cann')",
    ))
    assert result["advisories"] == []


def test_advisory_never_blocks_and_reports_detail() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/x.py",
        "        def compat_shim(self):",
    ))
    assert result["violations"] == []
    assert "non-blocking" in result["detail"]
    assert _check_maintainability([])["advisories"] == []
