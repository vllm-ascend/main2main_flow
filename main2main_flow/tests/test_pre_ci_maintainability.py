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


def _new_file_lines(file: str, *texts: str) -> list[dict[str, str]]:
    return [{"file": file, "line_no": str(i + 1), "text": t, "new_file": "1"}
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


# --- rule 2: new environment variables -------------------------------------

def test_new_env_key_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/envs.py",
        '    "VLLM_ASCEND_INDEX_SCORE_IMPL": lambda: os.getenv('
        '"VLLM_ASCEND_INDEX_SCORE_IMPL", "auto"),',
    ))
    assert result["violations"] == []
    assert _rules(result) == ["new_env_var"]
    assert result["advisories"][0]["file"] == "vllm_ascend/envs.py"


def test_new_env_getenv_read_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/models/minimax_m3/msa_m3.py",
        '        impl = os.getenv("VLLM_ASCEND_INDEX_SCORE_IMPL", "auto")',
    ))
    assert _rules(result) == ["new_env_var"]


def test_env_key_with_justification_marker_is_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/envs.py",
        "    # main2main-justification: cann<9.3 has no MsaIndexScore op",
        '    "VLLM_ASCEND_INDEX_SCORE_IMPL": lambda: os.getenv('
        '"VLLM_ASCEND_INDEX_SCORE_IMPL", "auto"),',
    ))
    assert result["advisories"] == []


def test_env_justification_marker_two_lines_above_still_counts() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/envs.py",
        "    # main2main-justification: upstream added the switch itself",
        "    # (continuation of the reason)",
        '    "VLLM_ASCEND_NEW_FLAG": lambda: os.getenv('
        '"VLLM_ASCEND_NEW_FLAG", "0"),',
    ))
    assert result["advisories"] == []


def test_env_justification_marker_three_lines_above_does_not_count() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/envs.py",
        "    # main2main-justification: too far away to apply",
        "    # padding",
        "    # padding",
        '    "VLLM_ASCEND_NEW_FLAG": lambda: os.getenv('
        '"VLLM_ASCEND_NEW_FLAG", "0"),',
    ))
    assert _rules(result) == ["new_env_var"]


def test_empty_justification_marker_does_not_count() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/envs.py",
        "    # main2main-justification:",
        '    "VLLM_ASCEND_NEW_FLAG": lambda: os.getenv('
        '"VLLM_ASCEND_NEW_FLAG", "0"),',
    ))
    assert _rules(result) == ["new_env_var"]


# --- rule 3: new patch modules ---------------------------------------------

def test_new_patch_file_is_advisory() -> None:
    result = _check_maintainability(_new_file_lines(
        "vllm_ascend/patch/platform/patch_compat.py",
        '"""Compat patch."""',
        "from vllm.something import thing",
    ))
    assert result["violations"] == []
    assert _rules(result) == ["new_patch_file"]
    assert result["advisories"][0]["line"] == "1"


def test_new_patch_file_with_header_justification_is_not_advisory() -> None:
    result = _check_maintainability(_new_file_lines(
        "vllm_ascend/patch/platform/patch_compat.py",
        "# main2main-justification: upstream moved the symbol to a new module",
        '"""Compat patch."""',
    ))
    assert result["advisories"] == []


def test_editing_an_existing_patch_file_is_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/patch/worker/patch_v2/patch_uva.py",
        "        if vllm_version_is(\"0.32.0\"):",
        "            ...",
    ))
    assert result["advisories"] == []


def test_new_patch_registration_is_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/patch/platform/__init__.py",
        'import vllm_ascend.patch.platform.patch_compat  # noqa',
        '    "vllm_ascend.patch.platform.patch_compat",',
    ))
    assert _rules(result) == ["new_patch_entry"]


def test_patch_registration_with_marker_is_not_advisory() -> None:
    result = _check_maintainability(_lines(
        "vllm_ascend/patch/platform/__init__.py",
        "    # main2main-justification: required by the new CANN contract",
        '    "vllm_ascend.patch.platform.patch_compat",',
    ))
    assert result["advisories"] == []


# --- diff parsing -----------------------------------------------------------

def test_parse_added_lines_marks_new_files() -> None:
    from main2main_flow.scripts.utils.pre_ci_check import _parse_added_lines

    diff = (
        "diff --git a/vllm_ascend/patch/platform/patch_x.py "
        "b/vllm_ascend/patch/platform/patch_x.py\n"
        "new file mode 100644\n"
        "index 000000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/vllm_ascend/patch/platform/patch_x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+first\n"
        "+second\n"
        "diff --git a/vllm_ascend/envs.py b/vllm_ascend/envs.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/vllm_ascend/envs.py\n"
        "+++ b/vllm_ascend/envs.py\n"
        "@@ -10,0 +11,1 @@\n"
        "+    edited\n"
    )
    entries = _parse_added_lines(diff)
    assert [(e["file"], e["line_no"], e["new_file"]) for e in entries] == [
        ("vllm_ascend/patch/platform/patch_x.py", "1", "1"),
        ("vllm_ascend/patch/platform/patch_x.py", "2", "1"),
        ("vllm_ascend/envs.py", "11", "0"),
    ]

