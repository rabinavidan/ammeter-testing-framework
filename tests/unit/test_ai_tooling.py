"""
The tooling around the AI agents: the agent-evaluation harness (planted defects and scoring) and the gate hook
that runs when the spec_gap_fixer agent stops. No agent is run here; this checks the measuring instruments.
"""
import glob
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import allure

from helpers import REPO

sys.path.insert(0, str(REPO / "evals"))
sys.path.insert(0, str(REPO / "tools" / "hooks"))
import after_fixer  # noqa: E402
import agent_evals  # noqa: E402


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def _part_the_evaluation_harness_plants_and_scores_known_defects():
    """Every planted defect still has its anchor in the code, and the audit and fix scores count what they claim."""
    defects = agent_evals.load_defects()
    for defect in defects:
        with allure.step(f"Defect '{defect['id']}': its anchor text is present once in {defect['file']}"):
            folder = defect["file"].split("/")[0]
            if "/" in defect["file"] and not os.path.isdir(os.path.join(REPO, folder)):
                # e.g. sample_results/ is left out of the Docker image (.dockerignore); checked wherever it exists
                allure.attach(f"{folder}/ is not part of this checkout", name="not checked here")
                continue
            paths = sorted(glob.glob(os.path.join(REPO, defect["file"])))
            assert paths, defect["file"]
            with open(paths[0], encoding="utf-8") as f:
                assert f.read().count(defect["find"]) == 1
    report = ("<!-- spec-gap: findings=2 gaps=0 high=1 -->\n## 3. Findings\n"
              "**F1 · WEAK · high: median of an even count is wrong (median_low)**\n"
              "**F2 · RISK · medium: README links to docs/design.md, which does not exist**\n"
              "## 4. Repository map\nencoding is fine\n")
    with allure.step("A report naming 2 of the planted defects in its findings scores recall 2/6; text outside the "
                     "findings (the repository map) doesn't count"):
        result = agent_evals.score_audit(report, defects)
        assert sorted(result["found"]) == ["broken-doc-link", "wrong-median"]
        assert result["recall"] == 2 / len(defects) and "missing-encoding" in result["missed"]
        assert result["reported_findings"] == 2 and result["hand_off"]


def _part_the_fix_score_needs_the_defects_gone_and_the_gates_green(tmp_path, monkeypatch):
    """A defect still present, or any red gate, means the fix run did not succeed."""
    defect = {"id": "d1", "file": "a.py", "find": "good", "replace": "bad", "detect": "x"}
    (tmp_path / "a.py").write_text("bad\n", encoding="utf-8")
    codes = {"ci": 0, "spec_audit": 0}

    def fake_run(command, cwd, capture_output):
        return SimpleNamespace(returncode=codes["spec_audit" if "spec_audit.py" in " ".join(command) else "ci"])

    monkeypatch.setattr(agent_evals.subprocess, "run", fake_run)
    with allure.step("Planted text still there -> remaining, not a success"):
        result = agent_evals.score_fix(str(tmp_path), [defect])
        assert result["remaining"] == ["d1"] and not result["success"]
    with allure.step("Fixed, gates green -> success"):
        (tmp_path / "a.py").write_text("good\n", encoding="utf-8")
        assert agent_evals.score_fix(str(tmp_path), [defect])["success"]
    with allure.step("Fixed, but the spec audit is red -> not a success"):
        codes["spec_audit"] = 1
        assert not agent_evals.score_fix(str(tmp_path), [defect])["success"]


def _part_the_gate_hook_blocks_the_fixer_only_while_gates_are_red(tmp_path, monkeypatch):
    """Exit 2 (keep working) when the fixer stops with red gates; exit 0 for other agents, green gates or a retry."""
    monkeypatch.setattr(after_fixer, "FIXES", str(tmp_path / "spec_gap_fixes.md"))
    monkeypatch.setattr(after_fixer, "STAMP", str(tmp_path / ".checked"))
    returncode = {"value": 1}
    monkeypatch.setattr(after_fixer.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode["value"], stdout="lint FAILED", stderr=""))

    def hook(event):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        return after_fixer.main()

    with allure.step("Another agent stops -> 0, no gate is run"):
        assert hook({"agent_type": "spec_gap_auditor"}) == 0
    with allure.step("The fixer stops with red gates -> 2, and the failure is shown to it"):
        assert hook({"agent_type": "spec_gap_fixer"}) == 2
    with allure.step("The retry after a block (stop_hook_active) -> 0, so it can stop and report the red state"):
        assert hook({"agent_type": "spec_gap_fixer", "stop_hook_active": True}) == 0
    with allure.step("Green gates -> 0"):
        returncode["value"] = 0
        assert hook({"agent_type": "spec_gap_fixer"}) == 0
    with allure.step("No agent name in the event: a fixes report newer than the last check identifies the fixer"):
        fixes, checked = tmp_path / "spec_gap_fixes.md", tmp_path / ".checked"
        fixes.write_text("fixes\n", encoding="utf-8")
        os.utime(checked, (100, 100))
        os.utime(fixes, (200, 200))
        assert after_fixer.is_fixer({})
    with allure.step("...and once checked after that report, the next stop is not treated as the fixer's"):
        os.utime(checked, (300, 300))
        assert not after_fixer.is_fixer({})


def test_the_ai_tooling(tmp_path, monkeypatch):
    """The agent-evaluation harness plants and scores defects correctly, and the gate hook blocks only on red gates."""
    with allure.step(
            "Every planted defect still has its anchor in the code, and the audit and fix scores count what they "
            "claim."):
        _part_the_evaluation_harness_plants_and_scores_known_defects()
    with allure.step("A defect still present, or any red gate, means the fix run did not succeed."):
        _part_the_fix_score_needs_the_defects_gone_and_the_gates_green(_subdir(tmp_path, 2), monkeypatch)
    with allure.step(
            "Exit 2 (keep working) when the fixer stops with red gates; exit 0 for other agents, green gates or a"
            " retry."):
        _part_the_gate_hook_blocks_the_fixer_only_while_gates_are_red(_subdir(tmp_path, 3), monkeypatch)
