"""
Claude Code SubagentStop hook: when the spec_gap_fixer agent tries to finish, run the gates it must leave green.

Registered as a SubagentStop hook in the Claude Code project
settings. It runs only for the fixer: the hook input names the agent, and when it
doesn't, a reports/spec_gap_fixes.md written since the last check identifies the fixer's run. If lint, the unit
layer or the spec audit is red, it exits 2: Claude Code then keeps the fixer working and shows it the failure.
It blocks once per stop attempt (`stop_hook_active` is set on the retry), so a fixer that can't get the gates green
still stops, and its report must say so.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXES = os.path.join(ROOT, "reports", "spec_gap_fixes.md")
STAMP = os.path.join(ROOT, "reports", ".fixer_gate_checked")


def interpreter() -> str:
    """The project's virtual environment if there is one (the gates need its packages)."""
    for candidate in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"),
                      os.path.join(ROOT, ".venv", "bin", "python")):
        if os.path.isfile(candidate):
            return candidate
    return sys.executable


def is_fixer(event: dict) -> bool:
    named = " ".join(str(event.get(key, "")) for key in ("agent_type", "subagent_type", "agent_name"))
    if named.strip():
        return "spec_gap_fixer" in named
    if not os.path.isfile(FIXES):
        return False
    checked = os.path.getmtime(STAMP) if os.path.isfile(STAMP) else 0
    return os.path.getmtime(FIXES) > checked


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if event.get("stop_hook_active") or not is_fixer(event):
        return 0
    os.makedirs(os.path.dirname(STAMP), exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as f:
        f.write("checked\n")
    python = interpreter()
    failures = []
    for name, command in (("lint + unit", [python, "tools/check.py", "lint", "unit"]),
                          ("spec audit", [python, "tools/spec_audit.py"])):
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if run.returncode != 0:
            failures.append(f"{name} failed (exit {run.returncode}):\n" + run.stdout[-1500:] + run.stderr[-500:])
    if failures:
        print("The gates are red after your fixes. Fix the cause (never weaken a test or a check), rerun the gates, "
              "and update reports/spec_gap_fixes.md:\n\n" + "\n\n".join(failures), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
