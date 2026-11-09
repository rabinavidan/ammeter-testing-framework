import os

import allure
import pytest

from examples.run_tests import main as run_example
from helpers import TEST_CONFIG_PATH, free_port


@pytest.mark.sanity
def test_the_example_script_runs_end_to_end(tmp_path, monkeypatch, capsys):
    """
    The demo script (fix #7) starts the emulators, tests all three, compares and assesses them, and stops
    its emulators. Run on free ports with the test config, in a temporary folder, so it is safe in CI.
    """
    monkeypatch.chdir(tmp_path)
    ports = {"greenlee": free_port(), "entes": free_port(), "circutor": free_port()}
    with allure.step(f"Run examples/run_tests.py main() on ports {ports}"):
        results = run_example(TEST_CONFIG_PATH, ports)
        output = capsys.readouterr().out
        allure.attach(output, name="output", attachment_type=allure.attachment_type.TEXT)
    with allure.step("Each ammeter got all its readings, and each run was archived"):
        assert sorted(results) == ["circutor", "entes", "greenlee"]
        for result in results.values():
            assert result["errors"] == [] and len(result["values"]) == result["sampling"]["measurements_count"]
            assert os.path.isfile(result["archive_path"])
    with allure.step("The output includes the accuracy report and the archive history"):
        assert "Accuracy assessment" in output and "Archive" in output
