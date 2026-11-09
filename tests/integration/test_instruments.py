"""Calibration sweeps over a real emulator answering from an instrument model (the unit layer covers the logic)."""
import allure

from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.instrument_model import InstrumentModel, ReferenceSource
from helpers import add_ammeter, attach_json, free_port
from src.testing.calibration import BoardLoad, format_sweep_table, run_accuracy_sweep, run_supply_sweep, sweep_points


def bench(framework, emulators, name, **overrides):
    """A source, a 10 A / 1 mA / 0.5 % + 3 counts meter (overridable), served by an emulator registered as `name`."""
    source = ReferenceSource()
    settings = {"range_a": 10, "resolution_a": 0.001, "accuracy_percent_of_reading": 0.5, "accuracy_counts": 3,
                "seed": 0}
    settings.update(overrides)
    instrument = InstrumentModel(source, **settings)
    add_ammeter(framework, name, emulators(GreenleeAmmeter(free_port(), instrument=instrument)),
                "MEASURE_GREENLEE -get_measurement")
    return source, instrument


def attach(sweep, name):
    allure.attach(format_sweep_table(sweep), name=name, attachment_type=allure.attachment_type.TEXT)
    attach_json(name, sweep)


def test_sweeps_over_a_real_emulator(framework, emulators):
    """Sweeps pass and fail for the right reasons when the meter is reached over the wire."""
    with allure.step("A good meter passes at 0, ±10, 50, 90, 100 % of range"):
        source, good = bench(framework, emulators, "good", gain_error=0.002, offset_a=0.001, noise_floor_a=0.0003)
        points = sweep_points({"range_a": 10, "test_signal": "dc"}, [0, 10, 50, 90, 100, -10, -100])
        sweep = run_accuracy_sweep(framework, "good", source, good, points, readings_per_point=5)
        attach(sweep, "good meter")
        assert sweep["passed"]
    with allure.step("A 1 % gain error passes near zero (0, 0.1 A) and fails the 0.5 % spec at 5 and 10 A"):
        source, off = bench(framework, emulators, "gain", gain_error=0.01)
        sweep = run_accuracy_sweep(framework, "gain", source, off, [0, 0.1, 5, 10], readings_per_point=3)
        assert [row["within_spec"] for row in sweep["points"]] == [True, True, False, False]
    with allure.step("A Rogowski coil tested on DC reads ≈ 0 and fails"):
        source, rogowski = bench(framework, emulators, "rogowski", range_a=100, resolution_a=0.01, measures_dc=False)
        sweep = run_accuracy_sweep(framework, "rogowski", source, rogowski, [10, 50], "dc", readings_per_point=3)
        assert all(abs(row["mean_a"]) < 0.05 for row in sweep["points"]) and not sweep["passed"]
    with allure.step("Supply sweep 0-12 V through 0.01 Ω: board off below 2.7 V, on from 3 V, everything in spec"):
        source, shunt = bench(framework, emulators, "shunt", burden_ohm=0.01, offset_a=0.0005)
        sweep = run_supply_sweep(framework, "shunt", source, shunt, BoardLoad(), [0, 2, 2.5, 3, 5, 12],
                                 readings_per_point=3)
        attach(sweep, "supply sweep")
        assert [row["board_on"] for row in sweep["points"]] == [False, False, False, True, True, True]
        assert sweep["passed"]
    with allure.step("25 µA of sleep current is below the 1 mA resolution: flagged, the sweep fails"):
        sleep = run_supply_sweep(framework, "shunt", source, shunt, BoardLoad(sleep_current_a=25e-6), [3.3],
                                 state="sleep", readings_per_point=3)
        assert sleep["points"][0]["below_resolution"] and not sleep["passed"]
