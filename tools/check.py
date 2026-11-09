"""
One entry point for every quality gate, so local runs, git hooks, CI and Docker run the same commands.

    python tools/check.py lint          # ruff, mypy (+ knowledge-base check where present)  (~5 s)
    python tools/check.py unit          # unit layer with coverage, fails under 90 %     (~8 s)
    python tools/check.py sanity        # hand-picked smoke set                          (~10 s)
    python tools/check.py regression    # every test except wall-clock `timing` ones     (~50 s; ~20 s --parallel)
    python tools/check.py timing        # wall-clock tests; run on a quiet machine
    python tools/check.py ci            # lint, unit, sanity, regression: what CI gates on
    python tools/check.py all           # ci + timing (nightly)

    --allure DIR    also write Allure results to DIR (test stages)
    --parallel      run the regression with pytest-xdist (-n auto)

Stages run in order and stop at the first failure; the exit code is that stage's.
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

UNIT_COVERAGE_FLOOR = 90       # the unit layer measures 93 % of the core code (26-test suite)


def commands(stage: str, allure_dir: str, parallel: bool):
    pytest = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    allure = [f"--alluredir={allure_dir}"] if allure_dir else []
    if stage == "lint":
        lint = [[PY, "-m", "ruff", "check", "."], [PY, "-m", "mypy"]]
        if os.path.isfile(os.path.join(ROOT, "tools", "kb.py")):     # internal knowledge base: local only
            lint.append([PY, "tools/kb.py", "check"])
        return lint
    if stage == "kb":
        return [[PY, "tools/kb.py", "check"]] if os.path.isfile(os.path.join(ROOT, "tools", "kb.py")) else []
    if stage == "unit":
        return [pytest + ["-m", "unit", "--cov=src", "--cov=Ammeters", "--cov-report=term",
                          "--cov-report=xml:coverage.xml", f"--cov-fail-under={UNIT_COVERAGE_FLOOR}"] + allure]
    if stage == "sanity":
        return [pytest + ["-m", "sanity"] + allure]
    if stage == "regression":
        return [pytest + ["-m", "regression and not timing"] + (["-n", "auto"] if parallel else []) + allure]
    if stage == "timing":
        return [pytest + ["-m", "timing"] + allure]
    raise ValueError(stage)


PIPELINES = {
    "ci": ["lint", "unit", "sanity", "regression"],
    "all": ["lint", "unit", "sanity", "regression", "timing"],
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the project's quality gates.")
    parser.add_argument("stages", nargs="+",
                        choices=["lint", "unit", "sanity", "regression", "timing", "kb", "ci", "all"])
    parser.add_argument("--allure", metavar="DIR", default="", help="write Allure results to DIR")
    parser.add_argument("--parallel", action="store_true", help="run the regression with pytest-xdist")
    args = parser.parse_args(argv)

    stages = [s for name in args.stages for s in PIPELINES.get(name, [name])]
    summary = []
    for stage in stages:
        started = time.monotonic()
        for command in commands(stage, args.allure, args.parallel):
            print(f"\n=== {stage}: {' '.join(command[1:] if command[0] == PY else command)}", flush=True)
            code = subprocess.call(command, cwd=ROOT)
            if code != 0:
                summary.append((stage, "FAILED", time.monotonic() - started))
                print_summary(summary)
                return code
        summary.append((stage, "passed", time.monotonic() - started))
    print_summary(summary)
    return 0


def print_summary(summary) -> None:
    print("\n=== summary")
    for stage, status, seconds in summary:
        print(f"  {stage:<11} {status:<7} {seconds:6.1f} s")


if __name__ == "__main__":
    sys.exit(main())
