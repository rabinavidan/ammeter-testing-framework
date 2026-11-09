"""
Evaluate the project's agents on known, planted defects, so a change to an agent's prompt is measured, not hoped.

    python evals/agent_evals.py plant  DIR      # copy the repo to DIR and plant every defect in evals/defects.json
    python evals/agent_evals.py prompt DIR      # the prompts to give spec_gap_auditor, then spec_gap_fixer, for DIR
    python evals/agent_evals.py audit  DIR      # score DIR/reports/spec_gap_report.md: which defects it found
    python evals/agent_evals.py fix    DIR      # score the fixer: which defects are gone, and are the gates green

Running the agents themselves is a Claude Code step (they are prompts, not Python): plant, start each agent with
the printed prompt (or `/spec-gap`) in a session opened at DIR, then score. Scores:

- audit recall = planted defects whose `detect` pattern appears in a finding of the report (GAP/WEAK/RISK text);
  extra findings are listed for a human to judge (they may be real issues, not false positives).
- fix rate = planted defects whose planted text is gone; plus the exit codes of `tools/check.py ci` and
  `tools/spec_audit.py` in DIR. A fix that leaves a gate red does not count as a success.

Each defect is one exact text substitution, so planting is reproducible and a fix is checkable without judgement.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFECTS = os.path.join(ROOT, "evals", "defects.json")
SKIP = {".venv", ".git", ".idea", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_AT_ROOT = {"results", "reports", "allure-results", "allure-report"}      # generated; `src/results` is code


def load_defects() -> List[Dict]:
    with open(DEFECTS, encoding="utf-8") as f:
        return json.load(f)


def targets(directory: str, defect: Dict) -> List[str]:
    return sorted(glob.glob(os.path.join(directory, defect["file"])))


def plant(directory: str) -> List[str]:
    """Copy the project (without generated or local state) and apply every defect. Returns the planted ids."""
    if os.path.exists(directory):
        raise SystemExit(f"{directory} already exists; use a new directory")
    def ignore(folder, names):
        at_root = os.path.samefile(folder, ROOT)
        return [n for n in names if n in SKIP or (at_root and n in SKIP_AT_ROOT)]

    shutil.copytree(ROOT, directory, ignore=ignore)
    planted = []
    for defect in load_defects():
        paths = targets(directory, defect)[:1]          # one file is enough for a glob target
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if defect["find"] not in text:
                raise SystemExit(f"defect {defect['id']}: anchor text not found in {path}; update evals/defects.json")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text.replace(defect["find"], defect["replace"], 1))
        planted.append(defect["id"])
    with open(os.path.join(directory, "evals", "PLANTED"), "w", encoding="utf-8") as f:
        f.write("\n".join(planted) + "\n")
    return planted


def findings_text(report: str) -> str:
    """The parts of the report that make claims of a problem: findings and non-OK matrix rows."""
    rows = [line for line in report.splitlines()
            if re.search(r"\b(GAP|WEAK|RISK)\b", line) or line.lstrip().startswith(("**F", "- **F", "### F"))]
    section = re.search(r"#+ *\d*\.? *Findings(.*?)(\n#+ *\d*\.? *Repository|\Z)", report, re.S | re.I)
    return "\n".join(rows) + ("\n" + section.group(1) if section else "")


def score_audit(report: str, defects: List[Dict]) -> Dict:
    text = findings_text(report).lower()
    found = [d["id"] for d in defects if re.search(d["detect"], text, re.I)]
    missed = [d["id"] for d in defects if d["id"] not in found]
    marker = re.search(r"<!-- spec-gap: findings=(\d+)", report)
    return {"recall": len(found) / len(defects), "found": found, "missed": missed,
            "reported_findings": int(marker.group(1)) if marker else None,
            "hand_off": "NEXT: run the spec_gap_fixer agent" in report or bool(marker and int(marker.group(1)))}


def score_fix(directory: str, defects: List[Dict]) -> Dict:
    remaining = []
    for defect in defects:
        for path in targets(directory, defect):
            with open(path, encoding="utf-8") as f:
                if defect["replace"] in f.read():
                    remaining.append(defect["id"])
                    break
    python = next((p for p in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
                               os.path.join(ROOT, ".venv", "bin", "python")) if os.path.isfile(p)), sys.executable)
    gates = {}
    for name, command in (("ci", [python, "tools/check.py", "ci", "--parallel"]),
                          ("spec_audit", [python, "tools/spec_audit.py"])):
        gates[name] = subprocess.run(command, cwd=directory, capture_output=True).returncode
    fixed = [d["id"] for d in defects if d["id"] not in remaining]
    return {"fix_rate": len(fixed) / len(defects), "fixed": fixed, "remaining": remaining, "gates": gates,
            "success": not remaining and all(code == 0 for code in gates.values())}


def prompts(directory: str) -> str:
    return (f"1. spec_gap_auditor: \"Audit the repository at {directory} (not the original project) against "
            f"Exam/ammeter-test-specification.pdf following your instructions; write {directory}/reports/"
            f"spec_gap_report.md.\"\n2. spec_gap_fixer: \"Fix the findings in {directory}/reports/spec_gap_report.md, "
            f"working only in {directory}, following your instructions.\"\nDon't tell either agent what was planted.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plant known defects and score the agents against them.")
    parser.add_argument("command", choices=["plant", "prompt", "audit", "fix"])
    parser.add_argument("directory")
    args = parser.parse_args(argv)
    directory = os.path.abspath(args.directory)
    defects = load_defects()
    if args.command == "plant":
        print("Planted:", ", ".join(plant(directory)))
        print(prompts(directory))
    elif args.command == "prompt":
        print(prompts(directory))
    elif args.command == "audit":
        with open(os.path.join(directory, "reports", "spec_gap_report.md"), encoding="utf-8") as f:
            result = score_audit(f.read(), defects)
        print(json.dumps(result, indent=2))
        return 0 if result["recall"] == 1 else 1
    elif args.command == "fix":
        result = score_fix(directory, defects)
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
