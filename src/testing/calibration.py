"""
Accuracy verification against a known current, as a calibration lab or production test would do it.

The ammeters are emulators given an InstrumentModel (Ammeters/instrument_model.py) and measuring a
shared ReferenceSource. Each point sets the source, takes readings through the normal
AmmeterTestFramework.run_test (so timing, error handling and archiving are the same as any test
run), and checks the mean reading against the instrument's datasheet tolerance. Each point also
gets a GUM-style uncertainty budget and the normalized error En against the reference.

Starting the emulators is the caller's job, as with run_test.
"""
import math
import statistics
from typing import Any, Dict, List, Sequence

from Ammeters.instrument_model import InstrumentModel, ReferenceSource

# Coverage factor for expanded uncertainty: k = 2 gives about 95 % coverage
COVERAGE_FACTOR = 2.0


def build_instruments(config: Dict[str, Any], source: ReferenceSource, seed: Any = 0) -> Dict[str, InstrumentModel]:
    """One InstrumentModel per entry in the config's accuracy_verification.instruments, seeded per instrument."""
    instruments = (config.get("accuracy_verification") or {}).get("instruments") or {}
    return {name: InstrumentModel.from_config(settings, source, seed=None if seed is None else f"{seed}-{name}")
            for name, settings in instruments.items()}


def sweep_points(instrument_config: Dict[str, Any], percents: Sequence[float]) -> List[float]:
    """
    Test currents in amps for percentages of an instrument's range. An AC test signal is an RMS
    value, which can't be negative, so negative points are dropped for AC instruments.
    """
    signal = instrument_config.get("test_signal", "dc")
    return [instrument_config["range_a"] * p / 100 for p in percents if signal == "dc" or p >= 0]


def evaluate_point(true_a: float, result: Dict, instrument: InstrumentModel, source: ReferenceSource) -> Dict:
    """
    Compare one run's readings with the true current.

    Pass/fail is the datasheet check: |mean - true| <= x % of reading + y counts. The uncertainty
    budget (GUM): Type A = std / sqrt(n) of the readings; Type B = quantization, resolution / sqrt(12);
    combined, then expanded with k = 2. En = error / sqrt(U_meter^2 + U_reference^2); |En| <= 1 means
    the meter agrees with the reference within the stated uncertainties. A meter can be within its
    datasheet spec yet have |En| > 1: its systematic error is larger than its own repeatability.
    """
    readings = result["values"]
    row: Dict[str, Any] = {
        "true_a": true_a,
        "test_id": result["test_id"],
        "readings": len(readings),
        "error_counts": result["error_counts"],
    }
    if not readings:
        row.update({"mean_a": None, "error_a": None, "tolerance_a": None, "within_spec": False, "en": None})
        return row

    mean = statistics.fmean(readings)
    error = mean - true_a
    tolerance = instrument.tolerance_a(mean) if instrument.has_spec() else None
    type_a = statistics.stdev(readings) / math.sqrt(len(readings)) if len(readings) >= 2 else 0.0
    type_b = instrument.resolution_uncertainty_a()
    combined = math.hypot(type_a, type_b)
    expanded_meter = COVERAGE_FACTOR * combined
    expanded_reference = COVERAGE_FACTOR * source.standard_uncertainty_a()
    denominator = math.hypot(expanded_meter, expanded_reference)
    row.update({
        "mean_a": mean,
        "error_a": error,
        "error_percent_of_range": 100 * error / instrument.range_a,
        "tolerance_a": tolerance,
        "within_spec": tolerance is not None and abs(error) <= tolerance,
        "uncertainty": {
            "type_a_a": type_a,
            "type_b_resolution_a": type_b,
            "combined_standard_a": combined,
            "expanded_k2_a": expanded_meter,
            "reference_expanded_k2_a": expanded_reference,
        },
        "en": error / denominator if denominator else None,
    })
    return row


def run_accuracy_sweep(framework, ammeter_type: str, source: ReferenceSource, instrument: InstrumentModel,
                       points_a: Sequence[float], signal: str = "dc", readings_per_point: int = 10,
                       sampling_frequency_hz: float = 50) -> Dict:
    """Set the source to each point, test the ammeter, and evaluate every point."""
    rows = []
    for point in points_a:
        source.set(point, signal)
        result = framework.run_test(ammeter_type, {"measurements_count": readings_per_point,
                                                   "sampling_frequency_hz": sampling_frequency_hz,
                                                   "total_duration_seconds": None})
        rows.append(evaluate_point(point, result, instrument, source))
    return {
        "ammeter_type": ammeter_type,
        "signal": signal,
        "spec": {"percent_of_reading": instrument.accuracy_percent_of_reading, "counts": instrument.accuracy_counts,
                 "resolution_a": instrument.resolution_a, "range_a": instrument.range_a},
        "points": rows,
        "passed": all(row["within_spec"] for row in rows),
    }


# --- supply sweep ---------------------------------------------------------------------------

class BoardLoad:
    """
    The embedded board from the specification's diagram, as a load on the 0-12 V supply: it draws
    nothing below its brown-out voltage, then a regulated (constant) current, active or asleep.
    """

    def __init__(self, brownout_v: float = 2.7, active_current_a: float = 0.045, sleep_current_a: float = 25e-6):
        if brownout_v <= 0 or active_current_a <= 0 or sleep_current_a < 0:
            raise ValueError("need brownout_v > 0, active_current_a > 0, sleep_current_a >= 0")
        self.brownout_v = brownout_v
        self.active_current_a = active_current_a
        self.sleep_current_a = sleep_current_a

    def operating_point(self, supply_v: float, burden_ohm: float, state: str = "active") -> Dict[str, Any]:
        """
        The ammeter sits in series with the board, so the board sees supply - I x burden. The board
        runs only if that is still above brown-out while drawing its current; otherwise it stays off.
        """
        if state not in ("active", "sleep"):
            raise ValueError(f"state must be 'active' or 'sleep', got {state!r}")
        current = self.active_current_a if state == "active" else self.sleep_current_a
        board_v = supply_v - current * burden_ohm
        if supply_v < self.brownout_v or board_v < self.brownout_v:
            return {"board_on": False, "current_a": 0.0, "board_v": supply_v, "burden_v": 0.0,
                    "browned_out_by_burden": supply_v >= self.brownout_v}
        return {"board_on": True, "current_a": current, "board_v": board_v, "burden_v": current * burden_ohm,
                "browned_out_by_burden": False}


def run_supply_sweep(framework, ammeter_type: str, source: ReferenceSource, instrument: InstrumentModel,
                     load: BoardLoad, voltages_v: Sequence[float], state: str = "active",
                     max_burden_fraction: float = 0.02, readings_per_point: int = 10,
                     sampling_frequency_hz: float = 50) -> Dict:
    """
    Sweep the supply voltage and measure the board's current at each step. Each row says whether
    the board was on, whether the meter's burden voltage was acceptable (at most max_burden_fraction
    of the supply) or even caused the brown-out, whether the reading met the meter's spec, and
    whether the current was below the meter's resolution (so the meter can't really see it).
    """
    rows = []
    for supply_v in voltages_v:
        point = load.operating_point(supply_v, instrument.burden_ohm, state)
        source.set(point["current_a"], "dc")
        result = framework.run_test(ammeter_type, {"measurements_count": readings_per_point,
                                                   "sampling_frequency_hz": sampling_frequency_hz,
                                                   "total_duration_seconds": None})
        row = evaluate_point(point["current_a"], result, instrument, source)
        row.update({
            "supply_v": supply_v,
            "board_v": point["board_v"],
            "board_on": point["board_on"],
            "browned_out_by_burden": point["browned_out_by_burden"],
            "burden_v": point["burden_v"],
            "burden_ok": supply_v == 0 or point["burden_v"] <= max_burden_fraction * supply_v,
            "below_resolution": 0 < point["current_a"] < instrument.resolution_a,
        })
        rows.append(row)
    return {
        "ammeter_type": ammeter_type,
        "state": state,
        "points": rows,
        "passed": all(r["within_spec"] and r["burden_ok"] and not r["browned_out_by_burden"]
                      and not r["below_resolution"] for r in rows),
    }


def format_sweep_table(sweep: Dict) -> str:
    """A plain-text table of a sweep, for logs and reports."""
    supply = "supply_v" in (sweep["points"][0] if sweep["points"] else {})
    # "spec" = within the datasheet tolerance (product acceptance); "|En|<=1" = agrees with the reference within
    # the combined uncertainty (calibration-lab agreement). Different questions, so both are shown.
    header = (["supply V", "board"] if supply else []) + ["true A", "mean A", "error A", "tol A", "spec", "En",
                                                           "|En|<=1"]
    lines = ["  ".join(f"{h:>10}" for h in header)]
    def fmt(value, digits=4):
        return "n/a" if value is None else f"{value:.{digits}g}"

    for r in sweep["points"]:
        cells = ([fmt(r["supply_v"], 3), "on" if r["board_on"] else "off"] if supply else [])
        cells += [fmt(r["true_a"]), fmt(r["mean_a"]), fmt(r["error_a"], 3), fmt(r["tolerance_a"], 3),
                  "PASS" if r["within_spec"] else "FAIL", fmt(r["en"], 3),
                  "n/a" if r["en"] is None else "yes" if abs(r["en"]) <= 1 else "no"]
        lines.append("  ".join(f"{c:>10}" for c in cells))
    en_values = [r["en"] for r in sweep["points"] if r["en"] is not None]
    agree = sum(abs(en) <= 1 for en in en_values)
    lines.append(f"overall (datasheet spec): {'PASS' if sweep['passed'] else 'FAIL'}; "
                 f"|En| <= 1 at {agree}/{len(en_values)} points")
    return "\n".join(lines)
