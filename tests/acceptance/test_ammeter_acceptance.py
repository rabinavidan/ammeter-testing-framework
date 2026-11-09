"""
Ammeter tests as the specification defines a test: measurement runs against the real emulators through the
framework, and calibration-style verification of the instrument models. Charts, statistics, control charts,
logs and sweep tables are attached to the Allure report, so the report doubles as the test results.
"""
import os

import allure
import pytest

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.instrument_model import ReferenceSource
from helpers import attach_json, free_port
from src.analysis.accuracy import format_accuracy_report
from src.testing.calibration import (
    BoardLoad,
    build_instruments,
    format_sweep_table,
    run_accuracy_sweep,
    run_supply_sweep,
    sweep_points,
)
from src.testing.test_framework import resolve_sampling

pytestmark = pytest.mark.acceptance

# Emulator class and the range its formula can produce (amps)
AMMETERS = {
    "greenlee": (GreenleeAmmeter, 1.0 / 100.0, 10.0 / 0.1),
    "entes": (EntesAmmeter, 0.01 * 500, 0.1 * 2000),
    "circutor": (CircutorAmmeter, 10 * 0.1 * 0.001, 10 * 1.0 * 0.01),
}


def test_the_three_ammeters_meet_the_specification(framework, emulators):
    """
    Each ammeter tested as the spec defines it (sampling, statistics, consistency, archive, charts), then the three
    compared: agreement, ranking and the most reliable method, stated as precision since no reference is set.
    """
    framework.configure({"testing": {"request_timeout_seconds": 2.0}, "analysis": {"visualization": {"enabled": True}}})
    count, duration, frequency = resolve_sampling(framework.config["testing"]["sampling"])
    results = {}
    for name, (cls, low, high) in AMMETERS.items():
        framework.configure({"ammeters": {name: {"port": emulators(cls(free_port()))}}})
        with allure.step(f"{name.upper()}: {count} readings at {frequency:g} Hz, all within {low:g}..{high:g} A"):
            result = framework.run_test(name)
            results[name] = result
            assert result["errors"] == [] and len(result["values"]) == count
            assert all(low <= v <= high for v in result["values"])
        with allure.step(f"{name.upper()}: lasted the configured {duration:g} s; statistics consistent; "
                         "control chart evaluated"):
            stats = result["statistics"]
            attach_json(f"{name} statistics", stats)
            assert duration <= result["actual_duration_seconds"] < duration + 0.5
            assert stats["min"] <= stats["median"] <= stats["max"] and stats["min"] <= stats["mean"] <= stats["max"]
            assert result["control_chart"]["lcl"] < result["control_chart"]["center"] < result["control_chart"]["ucl"]
        with allure.step(f"{name.upper()}: archived under a unique ID and retrievable; charts saved"):
            assert framework.archive.load(result["test_id"])["values"] == result["values"]
            assert len(result["plots"]) == len(framework.plot_types)
            assert all(os.path.isfile(p) for p in result["plots"])
    with allure.step("All three compared on one chart, assessed for precision, with a most reliable method"):
        assert os.path.isfile(framework.plot_comparison(list(results.values())))
        assessment = framework.assess_accuracy(list(results.values()))
        allure.attach(format_accuracy_report(assessment), name="accuracy report",
                      attachment_type=allure.attachment_type.TEXT)
        assert set(assessment["per_type"]) == set(AMMETERS) and assessment["most_reliable"] in AMMETERS
        assert assessment["ranking_metric"] == "robust_cv" and any("not accuracy" in n for n in assessment["notes"])


def test_the_instruments_meet_their_datasheets_and_the_board_sweep(framework, emulators):
    """
    Calibration-style verification from the config: each instrument across its range (both polarities for DC, RMS
    for the AC-only Rogowski coil) against ±(x % of reading + y counts), then the specification diagram's
    0-12 V board supply sweep through the meter.
    """
    settings = framework.config["accuracy_verification"]
    source = ReferenceSource(relative_uncertainty=settings["reference_relative_uncertainty"])
    instruments = build_instruments(framework.config, source, seed=settings["seed"])
    for name, model in instruments.items():
        cls, _, _ = AMMETERS[name]
        framework.register_ammeter(f"{name}_instrument", emulators(cls(free_port(), instrument=model)),
                                   framework.config["ammeters"][name]["command"])
    for name, spec in settings["instruments"].items():
        points = sweep_points(spec, settings["points_percent_of_range"])
        with allure.step(f"{name.upper()} ({spec['test_signal'].upper()}): within spec at {points} A"):
            sweep = run_accuracy_sweep(framework, f"{name}_instrument", source, instruments[name], points,
                                       spec["test_signal"], settings["readings_per_point"],
                                       settings["sampling_frequency_hz"])
            allure.attach(format_sweep_table(sweep), name=f"{name} sweep", attachment_type=allure.attachment_type.TEXT)
            failing = [row["true_a"] for row in sweep["points"] if not row["within_spec"]]
            assert failing == [], f"out of spec at {failing} A"
    supply = settings["supply_sweep"]
    load = BoardLoad(**supply["board"])
    with allure.step(f"Board supply sweep {supply['voltages_v']} V via {supply['instrument'].upper()}: off below "
                     f"{load.brownout_v} V, on above, readings in spec, burden within "
                     f"{supply['max_burden_fraction']:.0%} of supply"):
        sweep = run_supply_sweep(framework, f"{supply['instrument']}_instrument", source,
                                 instruments[supply["instrument"]], load, supply["voltages_v"],
                                 max_burden_fraction=supply["max_burden_fraction"],
                                 readings_per_point=settings["readings_per_point"],
                                 sampling_frequency_hz=settings["sampling_frequency_hz"])
        allure.attach(format_sweep_table(sweep), name="supply sweep", attachment_type=allure.attachment_type.TEXT)
        assert all(row["board_on"] == (row["supply_v"] >= load.brownout_v) for row in sweep["points"])
        assert sweep["passed"]
