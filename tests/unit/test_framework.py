"""
AmmeterTestFramework's own logic with a fake transport and a virtual clock: results, archiving, charts, failure
handling, configuration and the cross-run features. No sockets, no waiting.
"""
import json
import logging
import os

import allure
import pytest

from helpers import TEST_CONFIG_PATH, FakeClock
from src.testing.test_framework import AmmeterTestFramework, deep_merge

SAMPLING = {"measurements_count": 4, "sampling_frequency_hz": 10, "total_duration_seconds": None}
RESULT_KEYS = {"test_id", "ammeter_type", "port", "command", "sampling", "start_time", "actual_duration_seconds",
               "measurements", "values", "errors", "error_counts", "statistics", "control_chart", "aliasing",
               "warnings", "timing", "log_file", "plots", "plot_error", "archive_path", "archive_error"}


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def make(tmp_path, monkeypatch, values=(10.0, 11.0, 12.0, 13.0), **overrides):
    """A framework in tmp_path whose transport replies `values` in a loop, on a virtual clock."""
    monkeypatch.chdir(tmp_path)
    state = {"i": 0}

    def transport(port, command, timeout, host="localhost"):
        state["i"] += 1
        return values[(state["i"] - 1) % len(values)]

    framework = AmmeterTestFramework(TEST_CONFIG_PATH, clock=FakeClock(), transport=transport, overrides=overrides)
    framework.register_ammeter("virtual", 1, "MEASURE")
    return framework


def _part_a_run_produces_a_complete_archived_result_with_charts(tmp_path, monkeypatch):
    """The result has every documented field; it is archived with metadata and charted when charts are on."""
    framework = make(tmp_path, monkeypatch, **{"analysis": {"visualization": {"enabled": True}}})
    result = framework.run_test("virtual", SAMPLING)
    with allure.step(f"All {len(RESULT_KEYS)} documented fields; ID '{result['test_id']}' starts with the ammeter"):
        assert RESULT_KEYS <= set(result) and result["test_id"].startswith("virtual_")
    with allure.step("Sampling below Nyquist for 50 Hz mains is recorded and warned about"):
        assert result["aliasing"]["nyquist_ok"] is False
        assert any("ripple will appear" in w for w in result["warnings"])
    with allure.step("Archived: loading it gives the same result plus metadata"):
        record = framework.archive.load(result["test_id"])
        assert {key: record[key] for key in result} == result
        assert record["metadata"]["config_path"] == TEST_CONFIG_PATH and result["archive_error"] is None
    with allure.step("The run's log is closed when it ends: the file can be deleted, the logger is gone"):
        os.remove(result["log_file"])
        assert f"test_{result['test_id']}" not in logging.Logger.manager.loggerDict
    with allure.step(f"One chart per configured type: {framework.plot_types}"):
        assert len(result["plots"]) == len(framework.plot_types) and all(os.path.isfile(p) for p in result["plots"])


def _part_output_failures_never_cost_the_readings(tmp_path, monkeypatch):
    """If neither the archive nor the charts can be written, the run still returns its readings and says why."""
    framework = make(tmp_path, monkeypatch, **{
        "analysis": {"visualization": {"enabled": True, "output_dir": "blocker/plots"}},
        "result_management": {"archive_dir": "blocker/archive"}})
    open("blocker", "w", encoding="utf-8").write("not a folder")
    result = framework.run_test("virtual", SAMPLING)
    with allure.step("Readings kept; plot_error and archive_error say what failed"):
        assert result["values"] == [10.0, 11.0, 12.0, 13.0]
        assert result["plots"] == [] and result["plot_error"]
        assert result["archive_path"] is None and result["archive_error"]
    with allure.step("Archiving switched off: nothing written, archive_path None"):
        framework.configure({"result_management": {"enabled": False, "archive_dir": "elsewhere"}})
        off = framework.run_test("virtual", SAMPLING)
        assert off["archive_path"] is None and not framework.archive.exists(off["test_id"])


def test_a_run_end_to_end(tmp_path, monkeypatch):
    """A run returns every documented field, is archived and charted, and no output failure costs a reading."""
    with allure.step(
            "The result has every documented field; it is archived with metadata and charted when charts are on."):
        _part_a_run_produces_a_complete_archived_result_with_charts(_subdir(tmp_path, 1), monkeypatch)
    with allure.step(
            "If neither the archive nor the charts can be written, the run still returns its readings and says "
            "why."):
        _part_output_failures_never_cost_the_readings(_subdir(tmp_path, 2), monkeypatch)


def _part_configuration_api(tmp_path, monkeypatch):
    """configure() merges and re-validates; register_ammeter is all-or-nothing; bad names fail before sampling."""
    framework = make(tmp_path, monkeypatch)
    with allure.step("configure: charts on with one type, archive off; untouched settings stay"):
        framework.configure({"analysis": {"visualization": {"enabled": True, "plot_types": ["histogram"]}},
                             "result_management": {"enabled": False}})
        assert framework.plots_enabled and framework.plot_types == ["histogram"] and not framework.archive_enabled
        assert framework.config["testing"]["sampling"]["sampling_frequency_hz"] == 20
    with allure.step("configure rejects an unknown chart type and leaves the framework unchanged"):
        with pytest.raises(ValueError, match="pie"):
            framework.configure({"analysis": {"visualization": {"plot_types": ["pie"]}}})
        assert framework.plot_types == ["histogram"]
        assert framework.config["analysis"]["visualization"]["plot_types"] == ["histogram"]
    before = json.dumps(framework.config["ammeters"], sort_keys=True)
    for bad in ([10, 5], [5], "5..200", [5, "200"], [True, 3]):
        with allure.step(f"register_ammeter with plausible range {bad!r} fails and leaves the config unchanged"):
            with pytest.raises(ValueError, match="plausible_range_a"):
                framework.register_ammeter("bad", 1, "MEASURE", bad)
            assert json.dumps(framework.config["ammeters"], sort_keys=True) == before
    invalid = [
        ({"testing": {"request_timeout_seconds": "abc"}}, "request_timeout_seconds"),
        ({"testing": {"request_timeout_seconds": -1}}, "request_timeout_seconds"),
        ({"testing": {"mains_frequency_hz": "50Hz"}}, "mains_frequency_hz"),
        ({"ammeters": {"virtual": {"port": "abc"}}}, "port must be a whole number"),
        ({"ammeters": {"virtual": {"port": 70000}}}, "port must be a whole number"),
        ({"ammeters": {"virtual": {"command": ""}}}, "command must be a non-empty string"),
        # Every other section is checked up front too, not when it is first used (spec gap F1)
        ({"analysis": {"statistical_metrics": ["mean", "modee"]}}, "Unknown statistical metrics"),
        ({"testing": {"sampling": {"measurements_count": -3}}}, "measurements_count must be a positive"),
        ({"analysis": {"accuracy": {"confidence_level": 5}}}, "confidence must be between 0 and 1"),
        ({"analysis": {"accuracy": {"bootstrap_resamples": 10}}}, "at least 100"),
        ({"analysis": {"accuracy": {"reference_current_a": 0}}}, "non-zero"),
        ({"analysis": {"accuracy": {"random_seed": "zero"}}}, "random_seed"),
        ({"result_management": {"enabled": "false"}}, "result_management.enabled must be true or false"),
        ({"analysis": {"visualization": {"enabled": "no"}}}, "visualization.enabled must be true or false"),
        ({"error_simulation": {"enabled": "yes"}}, "error_simulation.enabled must be true or false"),
        ({"analysis": {"control_chart": {"sigma_limit": -1}}}, "control_chart.sigma_limit"),
        ({"analysis": {"control_chart": {"run_length": 1}}}, "control_chart.run_length"),
        ({"error_simulation": {"enabled": True, "faults": {"gremlins": {}}}}, "Unknown fault"),
        ({"error_simulation": {"enabled": True, "faults": {"no_response": {"probability": 0.1,
                                                                         "delay_seconds": 0.1}}}},
         "must be longer than testing.request_timeout_seconds"),
    ]
    for override, message in invalid:
        with allure.step(f"configure({override}) is rejected naming the setting; nothing changes"):
            config_before = json.dumps(framework.config, sort_keys=True)
            with pytest.raises(ValueError, match=message):
                framework.configure(override)
            assert json.dumps(framework.config, sort_keys=True) == config_before
    with allure.step("Timeout and mains frequency may be null (no timeout / aliasing check off)"):
        framework.configure({"testing": {"request_timeout_seconds": None, "mains_frequency_hz": None}})
    with allure.step("An unknown ammeter fails before any reading is taken"):
        with pytest.raises(ValueError, match="Unknown ammeter type"):
            framework.run_test("fluke")
    with allure.step("A name that can't be part of a test ID ('my meter') is refused at registration, not at "
                     "run_test"):
        with pytest.raises(ValueError, match="may only contain letters, digits"):
            framework.register_ammeter("my meter", 1, "MEASURE")
        assert "my meter" not in framework.config["ammeters"]
    with allure.step("The log folder is configurable (logging.output_dir) and the log path uses one separator"):
        framework.configure({"logging": {"output_dir": "logs/runs"}})
        log_file = framework.run_test("virtual", SAMPLING)["log_file"]
        assert os.path.dirname(log_file) == os.path.join("logs", "runs") and os.path.isfile(log_file)
    with allure.step("deep_merge: nested settings merge, the rest is replaced, inputs unchanged"):
        base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
        assert deep_merge(base, {"a": {"y": 3}, "b": [9]}) == {"a": {"x": 1, "y": 3}, "b": [9]}
        assert base == {"a": {"x": 1, "y": 2}, "b": [1, 2]}


def _part_cross_run_features_work_from_the_archive(tmp_path, monkeypatch):
    """The accuracy assessment and comparison chart work over archived runs."""
    framework = make(tmp_path, monkeypatch, values=(10.0, 10.2, 9.9, 10.1))
    framework.register_ammeter("second", 1, "MEASURE")
    framework.run_test("virtual", SAMPLING)
    first, second = framework.run_test("virtual", SAMPLING), framework.run_test("second", SAMPLING)
    with allure.step("Default assessment: the latest run of each configured ammeter only, report saved"):
        assessment = framework.assess_accuracy()
        assert set(assessment["per_type"]) == {"virtual", "second"} and os.path.isfile(assessment["report_path"])
        assert assessment["runs_used"] == [first["test_id"], second["test_id"]]
        assert assessment["per_type"]["virtual"]["runs"] == 1
    with allure.step("A run of an ammeter this framework doesn't configure (archived by another) is left out"):
        other = make(tmp_path, monkeypatch)                  # same folder, so the same archive
        other.register_ammeter("adhoc", 1, "MEASURE")
        other.run_test("adhoc", SAMPLING)
        assert "adhoc" in other.assess_accuracy(save=False)["per_type"]
        assert "adhoc" not in framework.assess_accuracy(save=False)["per_type"]
    with allure.step("Draw the comparison chart of both runs"):
        assert os.path.isfile(framework.plot_comparison([first, second]))


def test_configuration_and_cross_run_features(tmp_path, monkeypatch):
    """configure() and register_ammeter() validate all-or-nothing; assessments and comparisons work from the archive."""
    with allure.step(
            "configure() merges and re-validates; register_ammeter is all-or-nothing; bad names fail before "
            "sampling."):
        _part_configuration_api(_subdir(tmp_path, 1), monkeypatch)
    with allure.step("The accuracy assessment and comparison chart work over archived runs."):
        _part_cross_run_features_work_from_the_archive(_subdir(tmp_path, 2), monkeypatch)
