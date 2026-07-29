"""CLI entry-point for ``kickoff`` console script."""
import argparse

from main2main_flow.flow import Main2MainFlow


def kickoff():
    parser = argparse.ArgumentParser(description="Run Main2Main Flow")
    parser.add_argument("--vllm-path", default=None,
                        help="Local path or GitHub URL for the vllm repo")
    parser.add_argument("--vllm-ascend-path", default=None,
                        help="Local path or GitHub URL for the vllm-ascend repo")
    parser.add_argument("--target-commit", default=None,
                        help="Target vllm commit SHA to upgrade to (default: vllm HEAD)")
    args = parser.parse_args()

    inputs = {}
    if args.vllm_path:
        inputs["vllm_path"] = args.vllm_path
    if args.vllm_ascend_path:
        inputs["vllm_ascend_path"] = args.vllm_ascend_path
    if args.target_commit:
        inputs["target_commit"] = args.target_commit

    flow = Main2MainFlow()
    flow.run(inputs if inputs else None)
