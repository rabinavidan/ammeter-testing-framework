"""
Diff coverage: which CHANGED lines of the core code no test executes.

    python tools/changed_coverage.py                       # changes in Ammeters/ vs HEAD, unit+integration tests
    python tools/changed_coverage.py --base origin/main    # changes on a branch (CI on pull requests)
    python tools/changed_coverage.py --path src --min 90   # another directory, a lower bar
    python tools/changed_coverage.py --layers unit         # only the unit layer must cover the change

Changed lines = `git diff -U0 <base>` for tracked files plus every line of new (untracked) files under --path.
In a repository with no commits yet, or outside git, every line under --path counts as changed. The tests run
once with coverage; the report lists each changed file, its changed executable lines, the uncovered ones with
their source, and exits 1 if the covered share is below --min (default 100 %). Used by the.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args: str) -> Optional[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def all_lines(path: str) -> Set[int]:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return set(range(1, sum(1 for _ in f) + 1))


def python_files(directory: str):
    for folder, _, files in os.walk(os.path.join(ROOT, directory)):
        for name in files:
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(folder, name), ROOT).replace("\\", "/")


def changed_lines(base: str, directory: str) -> Dict[str, Set[int]]:
    """{file: changed line numbers} for .py files under `directory`."""
    if git("rev-parse", "--is-inside-work-tree") is None or git("rev-parse", "--verify", "HEAD") is None:
        print(f"(no git history: treating every line under {directory}/ as changed)")
        return {path: all_lines(path) for path in python_files(directory)}

    changes: Dict[str, Set[int]] = {}
    diff = git("diff", "-U0", base, "--", directory) or ""
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and current and current.endswith(".py"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                changes.setdefault(current, set()).update(range(start, start + count))
    for path in (git("ls-files", "--others", "--exclude-standard", "--", directory) or "").splitlines():
        if path.endswith(".py"):
            changes[path] = all_lines(path)
    return changes


def run_coverage(directory: str, layers: str, report_path: str) -> int:
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", layers,
               f"--cov={directory}", f"--cov-report=json:{report_path}"]
    print("running:", " ".join(command[1:]), flush=True)
    return subprocess.call(command, cwd=ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Coverage of changed lines in the core code.")
    parser.add_argument("--base", default="HEAD", help="git ref to diff against (default HEAD: uncommitted work)")
    parser.add_argument("--path", default="Ammeters", help="directory whose changes must be covered")
    parser.add_argument("--layers", default="unit or integration", help="pytest -m expression for the tests to run")
    parser.add_argument("--min", type=float, default=100.0, help="required covered share of changed lines, %%")
    args = parser.parse_args(argv)

    changes = changed_lines(args.base, args.path)
    if not changes:
        print(f"No changed Python lines under {args.path}/ (vs {args.base}). Nothing to cover.")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "coverage.json")
        tests_exit = run_coverage(args.path, args.layers, report)
        if not os.path.exists(report):
            print("Coverage report missing: the test run failed before measuring.")
            return tests_exit or 1
        data = json.load(open(report, encoding="utf-8"))["files"]

    files = {path.replace("\\", "/"): info for path, info in data.items()}
    total = covered = 0
    print(f"\nChanged lines under {args.path}/ (vs {args.base}):")
    for path in sorted(changes):
        info = files.get(path)
        if info is None:
            print(f"  {path}: not imported by any test (0 % of its changes covered)")
            executable = changes[path]      # can't tell which lines are code; count every changed line
            missing = executable
        else:
            executable = changes[path] & (set(info["executed_lines"]) | set(info["missing_lines"]))
            missing = changes[path] & set(info["missing_lines"])
        if not executable:
            continue
        total += len(executable)
        covered += len(executable) - len(missing)
        share = 100 * (len(executable) - len(missing)) / len(executable)
        print(f"  {path}: {len(executable) - len(missing)}/{len(executable)} changed executable lines covered "
              f"({share:.0f} %)")
        if missing and info is not None:
            source = open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines()
            for number in sorted(missing):
                print(f"      line {number:4}: {source[number - 1].strip()}")

    if total == 0:
        print("No executable lines changed (comments, docstrings or blank lines only).")
        return tests_exit
    share = 100 * covered / total
    print(f"\nChanged-line coverage: {covered}/{total} = {share:.1f} % (required {args.min:g} %)")
    if tests_exit:
        print(f"Note: the tests themselves failed (exit {tests_exit}); fix them first.")
        return tests_exit
    return 0 if share >= args.min else 1


if __name__ == "__main__":
    sys.exit(main())
