"""
The calibration sweeps without sockets: the framework's transport reads straight from the instrument model, on a
virtual clock. The same sweeps over real emulators are in tests/integration/test_instruments.py.
"""

import math

import allure
import pytest

from Ammeters.client import OVER_RANGE, OverRangeError
from Ammeters.instrument_model import InstrumentModel, ReferenceSource
from helpers import TEST_CONFIG_PATH, FakeClock, attach_json
from src.testing.calibration import (
    BoardLoad,
    build_instruments,
    evaluate_point,
    format_sweep_table,
    run_accuracy_sweep,
    run_supply_sweep,
    sweep_points,
)
from src.testing.test_framework import AmmeterTestFramework


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def meter(source, **overrides):
    """A 10 A, 1 mA-resolution meter with a 0.5 % + 3 counts spec and no errors unless overridden."""
    settings = {"range_a": 10, "resolution_a": 0.001, "accuracy_percent_of_reading": 0.5, "accuracy_counts": 3,
                "seed": 0}
    settings.update(overrides)
    return InstrumentModel(source, **settings)


def _part_the_instrument_model_reads_like_a_real_meter():
    """Quantization, gain/offset, polarity, over-range, Rogowski DC blindness, seeded noise, datasheet tolerance."""
    source = ReferenceSource(1.23456)
    with allure.step("1.23456 A shows as '1.235' at 1 mA resolution and '1.2' at 0.1 A"):
        assert meter(source).read_reply() == "1.235"
        assert meter(source, range_a=200, resolution_a=0.1).read_reply() == "1.2"
    with allure.step("+1 % gain and +2 mA offset turn 5 A into '5.052'"):
        source.set(5.0)
        assert meter(source, gain_error=0.01, offset_a=0.002).read_reply() == "5.052"
    with allure.step("-2.5 A reads '-2.500'; a tiny negative offset at 0 reads '0.000', not '-0.000'"):
        source.set(-2.5)
        assert meter(source).read_reply() == "-2.500"
        source.set(0)
        assert meter(source, offset_a=-0.0001).read_reply() == "0.000"
    with allure.step("10.4 A is within the 5 % headroom; 10.6 A and -12 A are 'OL'"):
        source.set(10.4)
        assert meter(source).read_reply() == "10.400"
        for current in (10.6, -12):
            source.set(current)
            assert meter(source).read_reply() == "OL"
    with allure.step("A Rogowski coil reads 0 on 50 A DC and 50 on 50 A AC (RMS)"):
        source.set(50.0, "dc")
        rogowski = meter(source, range_a=100, resolution_a=0.01, measures_dc=False)
        assert rogowski.read_reply() == "0.00"
        source.set(50.0, "ac")
        assert rogowski.read_reply() == "50.00"
    with allure.step("Same seed, same noise; tolerance = 0.5 % of reading + 3 counts (8 A -> 43 mA, 0 A -> 3 mA)"):
        source.set(1.0, "dc")
        a, b = (meter(source, noise_floor_a=0.001, seed=7) for _ in range(2))
        assert [a.read_reply() for _ in range(20)] == [b.read_reply() for _ in range(20)]
        assert meter(source).tolerance_a(8.0) == pytest.approx(0.043)
        assert meter(source).tolerance_a(0.0) == pytest.approx(0.003)


def _part_invalid_instruments_and_sources_are_rejected():
    """Impossible settings fail when the model or source is built; a failed change leaves the source as it was."""
    for settings, message in [({"range_a": 0}, "resolution_a < range_a"), ({"resolution_a": 20}, "resolution_a"),
                              ({"noise_floor_a": -1}, "must not be negative"), ({"accuracy_counts": None}, "both")]:
        with allure.step(f"meter({settings}) -> ValueError '{message}'"):
            with pytest.raises(ValueError, match=message):
                meter(ReferenceSource(), **settings)
    with allure.step("Unknown config key ('range') -> ValueError; a meter without a spec has no tolerance"):
        with pytest.raises(ValueError, match="Unknown instrument settings"):
            InstrumentModel.from_config({"range": 10, "resolution_a": 0.001}, ReferenceSource())
        unspecified = InstrumentModel(ReferenceSource(), range_a=10, resolution_a=0.001)
        assert not unspecified.has_spec()
        with pytest.raises(ValueError, match="no accuracy spec"):
            unspecified.tolerance_a(8.0)
    for kwargs in ({"signal": "rf"}, {"signal": "DC"}, {"relative_uncertainty": -1e-4}):
        with allure.step(f"ReferenceSource({kwargs}) -> ValueError"):
            with pytest.raises(ValueError):
                ReferenceSource(2.0, **kwargs)
    with allure.step("set(5.0, 'rf') is refused and the source still outputs 2.0 A DC; zero uncertainty is allowed"):
        source = ReferenceSource(2.0)
        with pytest.raises(ValueError, match="signal"):
            source.set(5.0, "rf")
        assert (source.current_a, source.signal) == (2.0, "dc")
        assert ReferenceSource(2.0, relative_uncertainty=0).standard_uncertainty_a() == 0


def _part_sweep_points_and_the_uncertainty_budget():
    """Points are percent of range (no negatives for AC); each point gets Type A/B uncertainty, U(k=2) and En."""
    with allure.step("DC, 10 A: 0, 1, 5, 10, -5 A; AC, 100 A: 0, 10, 50, 100 A"):
        percents = [0, 10, 50, 100, -50]
        assert sweep_points({"range_a": 10, "test_signal": "dc"}, percents) == [0, 1, 5, 10, -5]
        assert sweep_points({"range_a": 100, "test_signal": "ac"}, percents) == [0, 10, 50, 100]
    source = ReferenceSource(5.0, relative_uncertainty=1e-4)
    readings = [5.010, 5.012, 5.011, 5.013]
    row = evaluate_point(5.0, {"test_id": "x", "values": readings, "error_counts": {}}, meter(source), source)
    attach_json("evaluated point", row)
    u, type_a = row["uncertainty"], 0.0012909944487358 / 2
    with allure.step("Type A = std/√4, Type B = 1 mA/√12, U = 2√(A²+B²), reference U = 0.01 % × 5 A"):
        assert u["type_a_a"] == pytest.approx(type_a)
        assert u["type_b_resolution_a"] == pytest.approx(0.001 / math.sqrt(12))
        assert u["expanded_k2_a"] == pytest.approx(2 * math.hypot(type_a, 0.001 / math.sqrt(12)))
        assert u["reference_expanded_k2_a"] == pytest.approx(0.0005)
    with allure.step(f"Error 11.5 mA is within the 28 mA spec, yet En = {row['en']:.1f} > 1"):
        assert row["error_a"] == pytest.approx(0.0115) and row["within_spec"] and row["en"] > 1


@pytest.mark.sanity
def test_the_instrument_model():
    """The instrument model reads like a real meter, rejects bad settings, and gives sweep points and uncertainty."""
    with allure.step(
            "Quantization, gain/offset, polarity, over-range, Rogowski DC blindness, seeded noise, datasheet "
            "tolerance."):
        _part_the_instrument_model_reads_like_a_real_meter()
    with allure.step(
            "Impossible settings fail when the model or source is built; a failed change leaves the source as it "
            "was."):
        _part_invalid_instruments_and_sources_are_rejected()
    with allure.step(
            "Points are percent of range (no negatives for AC); each point gets Type A/B uncertainty, U(k=2) and "
            "En."):
        _part_sweep_points_and_the_uncertainty_budget()


def bench(tmp_path, monkeypatch, **instrument_settings):
    """A framework whose 'meter' reads from an InstrumentModel directly (no emulator, no socket, virtual time)."""
    monkeypatch.chdir(tmp_path)
    source = ReferenceSource()
    settings = {"range_a": 10, "resolution_a": 0.001, "accuracy_percent_of_reading": 0.5, "accuracy_counts": 3,
                "seed": 0}
    settings.update(instrument_settings)
    instrument = InstrumentModel(source, **settings)

    def transport(port, command, timeout, host="localhost"):
        reply = instrument.read_reply()
        if reply == OVER_RANGE:
            raise OverRangeError("meter reported over-range (OL)")
        return float(reply)

    framework = AmmeterTestFramework(TEST_CONFIG_PATH, clock=FakeClock(), transport=transport,
                                     overrides={"result_management": {"enabled": False}})
    framework.register_ammeter("meter", 1, "MEASURE")
    return framework, source, instrument


def _part_accuracy_sweep(tmp_path, monkeypatch):
    """A good meter passes at every point incl. zero and negatives; a point beyond the range is OL and fails."""
    framework, source, instrument = bench(tmp_path, monkeypatch, gain_error=0.002, offset_a=0.001)
    points = [0, 1, 5, 9, 10, -1, -10]
    with allure.step(f"Sweep {points} A: every point within ±(0.5 % + 3 counts)"):
        sweep = run_accuracy_sweep(framework, "meter", source, instrument, points, readings_per_point=4)
        allure.attach(format_sweep_table(sweep), name="sweep", attachment_type=allure.attachment_type.TEXT)
        assert all(row["within_spec"] for row in sweep["points"]) and sweep["passed"]
    with allure.step("At 10 A the error is the gain (0.2 %) plus the 1 mA offset: 21 mA"):
        assert sweep["points"][4]["error_a"] == pytest.approx(0.021, abs=0.001)
    with allure.step("12 A on a 10 A meter: no readings, 3 OverRangeError, the point fails"):
        over = run_accuracy_sweep(framework, "meter", source, instrument, [12], readings_per_point=3)
        row = over["points"][0]
        assert row["readings"] == 0 and row["error_counts"] == {"OverRangeError": 3}
        assert row["within_spec"] is False and row["en"] is None and not over["passed"]
    with allure.step("The table shows an |En| <= 1 verdict per point next to the spec verdict, and counts both "
                     "in its last line, so 'PASS' isn't read as agreement with the reference"):
        table = format_sweep_table(sweep).splitlines()
        assert table[0].split()[-3:] == ["spec", "En", "|En|<=1"]
        verdicts = [line.split()[-1] for line in table[1:-1]]
        expected = ["yes" if abs(r["en"]) <= 1 else "no" for r in sweep["points"]]
        assert verdicts == expected
        agree = f"{expected.count('yes')}/{len(expected)}"
        assert table[-1] == f"overall (datasheet spec): PASS; |En| <= 1 at {agree} points"
        assert format_sweep_table(over).splitlines()[1].split()[-1] == "n/a"


def _part_supply_sweep_and_its_inputs(tmp_path, monkeypatch):
    """The diagram's supply sweep: board state and burden per point; a high burden browns the board out."""
    framework, source, instrument = bench(tmp_path, monkeypatch, burden_ohm=0.01)
    with allure.step("2 V: board off, 0 A; 5 V: on, 45 mA through 0.45 mV of burden; both pass"):
        sweep = run_supply_sweep(framework, "meter", source, instrument, BoardLoad(), [2.0, 5.0],
                                 readings_per_point=3)
        attach_json("supply sweep", sweep)
        off, on = sweep["points"]
        assert not off["board_on"] and off["true_a"] == 0
        assert on["board_on"] and on["true_a"] == 0.045 and on["burden_v"] == pytest.approx(0.00045)
        assert sweep["passed"]
    with allure.step("A 10 Ω burden at 3 V: 0.45 V drop puts the board under its 2.7 V brown-out; the sweep fails"):
        framework, source, heavy = bench(tmp_path, monkeypatch, burden_ohm=10)
        browned = run_supply_sweep(framework, "meter", source, heavy, BoardLoad(), [3.0], readings_per_point=2)
        assert browned["points"][0]["browned_out_by_burden"] and not browned["passed"]
    with allure.step("The test config's datasheets build: Greenlee 10 A / 1 mA / 0.5 % + 3; CIRCUTOR blind to DC"):
        from src.utils.config import load_config
        instruments = build_instruments(load_config(TEST_CONFIG_PATH), ReferenceSource())
        greenlee = instruments["greenlee"]
        assert (greenlee.range_a, greenlee.resolution_a, greenlee.accuracy_percent_of_reading,
                greenlee.accuracy_counts) == (10, 0.001, 0.5, 3)
        assert instruments["circutor"].measures_dc is False
    with allure.step("A board with zero brown-out or no active current, or an unknown state, is rejected"):
        with pytest.raises(ValueError):
            BoardLoad(brownout_v=0)
        with pytest.raises(ValueError):
            BoardLoad(active_current_a=0)
        with pytest.raises(ValueError, match="state"):
            BoardLoad().operating_point(5, 0, state="hibernate")


def test_the_calibration_sweeps(tmp_path, monkeypatch):
    """The accuracy sweep and the supply sweep, read through the normal run_test on a virtual clock."""
    with allure.step(
            "A good meter passes at every point incl. zero and negatives; a point beyond the range is OL and "
            "fails."):
        _part_accuracy_sweep(_subdir(tmp_path, 1), monkeypatch)
    with allure.step(
            "The diagram's supply sweep: board state and burden per point; a high burden browns the board out."):
        _part_supply_sweep_and_its_inputs(_subdir(tmp_path, 2), monkeypatch)
