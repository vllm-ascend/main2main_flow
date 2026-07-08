# Versioning Primer

How vllm-ascend supports two vLLM versions in one codebase.

- `vllm_version_is("<tag>")` (defined in `vllm_ascend/utils.py`) is True iff the
  installed vLLM version equals `<tag>` exactly
  (`Version(vllm.__version__) == Version(tag)`; the `VLLM_VERSION` env var
  overrides detection, see `vllm_ascend/envs.py`).
- "Release" = the pinned vLLM release the plugin officially supports. The tag
  lives in `.github/vllm-release-tag.commit` (e.g. `v0.23.0`) and is exported as
  `main_vllm_tag` by `docs/source/conf.py`.
- "Upstream main" = the newest verified vLLM main commit, pinned in
  `.github/vllm-main-verified.commit`. main2main advances THIS pin every run;
  the release tag advances only when vllm-ascend adopts a new vLLM release.
- Direction rule: when running against upstream main, `vllm.__version__` is a
  dev version, so `vllm_version_is("<release_tag>")` is False. Therefore:
  - release-only behavior → inside `if vllm_version_is("<release_tag>"):`
  - new upstream-main behavior → the `else` / `if not vllm_version_is(...)` branch
  Existing example: `vllm_ascend/patch/platform/__init__.py` imports
  release-only tool-parser patches under `if vllm_version_is("0.23.0"):`.
- Existing call sites all pass the bare release version (`"0.23.0"`); use the
  release tag given in the prompt, never invent another version string.
- Guards are removed only when the release tag advances — see the
  "Guard lifecycle" section of `adapt-guide.md`.
