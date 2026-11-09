import statistics
import time

import allure
import pytest

from Ammeters.client import request_current_from_ammeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.error_simulation import TRANSPORT_FAULTS, ErrorSimulator
from helpers import FixedAmmeter, add_ammeter, attach_json, free_port, run
from src.analysis.accuracy import assess_accuracy


def fixed_with(framework, emulators, faults, name):
    """A 10 A FixedAmmeter with these faults, registered as `name`; returns the simulator and the port."""
    simulator = ErrorSimulator(faults, seed=1)
    port = emulators(FixedAmmeter(free_port(), simulator))
    add_ammeter(framework, name, port)
    return simulator, port


def sampling(n, frequency=50):
    return {"measurements_count": n, "sampling_frequency_hz": frequency, "total_duration_seconds": None}


def test_lost_replies_are_detected_one_for_one(framework, emulators):
    """Each lost-reply fault on every request is detected as its error type; mixed faults: injected = detected."""
    for fault, params, error_types in [("no_response", {"delay_seconds": 0.8}, {"TimeoutError"}),
                                       ("dropped_connection", {}, {"NoReplyError"}),
                                       ("corrupted_response", {}, {"ValueError", "UnicodeDecodeError"}),
                                       ("truncated_response", {}, {"TruncatedReplyError"})]:
        with allure.step(f"{fault} on every request: 6/6 readings fail as {sorted(error_types)}; device recovers"):
            simulator, port = fixed_with(framework, emulators, {fault: {"probability": 1, **params}}, fault)
            result = run(framework, fault, sampling(6))
            assert result["values"] == [] and set(result["error_counts"]) <= error_types
            assert sum(result["error_counts"].values()) == 6
            # The emulator serves one connection at a time: let it work through requests queued behind a hang
            deadline = time.monotonic() + 10
            while simulator.counts()[fault] < 6 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert simulator.counts()[fault] == 6
            simulator.faults[fault]["probability"] = 0
            assert request_current_from_ammeter(port, b"MEASURE_FIXED", timeout=2) == 10.0
    with allure.step("Mixed faults over 150 ENTES readings: lost replies injected = failed readings detected"):
        simulator = ErrorSimulator({
            "no_response": {"probability": 0.05, "delay_seconds": 0.6}, "dropped_connection": {"probability": 0.05},
            "corrupted_response": {"probability": 0.05}, "truncated_response": {"probability": 0.05},
            "latency": {"probability": 0.2, "delay_seconds": 0.02},
            "fragmented_response": {"probability": 0.2, "gap_seconds": 0.01}}, seed=3)
        add_ammeter(framework, "mixed", emulators(EntesAmmeter(free_port(), simulator)), "MEASURE_ENTES -get_data")
        result = run(framework, "mixed", sampling(150))
        counts = simulator.counts()
        attach_json("injected", counts)
        lost = sum(counts[f] for f in TRANSPORT_FAULTS)
        assert lost > 0 and counts["fragmented_response"] > 0
        assert len(result["errors"]) == lost and len(result["values"]) == counts["requests"] - lost


def test_measurement_faults_distort_readings_as_configured(framework, emulators):
    """Offset, drift, noise, spikes and mains ripple change valid readings exactly as configured."""
    with allure.step("5 % offset: every 10 A reading is exactly 10.5 A"):
        fixed_with(framework, emulators, {"offset": {"relative": 0.05}}, "offset")
        assert run(framework, "offset", sampling(5))["values"] == pytest.approx([10.5] * 5)
    with allure.step("1 %/reading drift: 10.0, 10.1, ..., 11.0 A"):
        fixed_with(framework, emulators, {"drift": {"relative_per_reading": 0.01}}, "drift")
        expected = [10 * (1 + 0.01 * i) for i in range(11)]
        assert run(framework, "drift", sampling(11))["values"] == pytest.approx(expected)
    with allure.step("2 % noise: mean ≈ 10 A, relative spread ≈ 0.02"):
        fixed_with(framework, emulators, {"noise": {"relative_sd": 0.02}}, "noise")
        values = run(framework, "noise", sampling(200))["values"]
        assert statistics.fmean(values) == pytest.approx(10, rel=0.01)
        assert statistics.stdev(values) / 10 == pytest.approx(0.02, rel=0.25)
    with allure.step("10 % spikes ×10: as many spikes as injected, every one flagged by the control chart"):
        simulator, _ = fixed_with(framework, emulators, {"spike": {"probability": 0.1, "factor": 10},
                                                         "noise": {"relative_sd": 0.01}}, "spike")
        result = run(framework, "spike", sampling(100))
        spikes = [i for i, v in enumerate(result["values"]) if v > 50]
        assert len(spikes) == simulator.counts()["spike"] > 0
        assert set(spikes) <= set(result["control_chart"]["beyond_limits"])
    with allure.step("An offset is invisible without a reference; against 10 A the bias is +5 %"):
        fixed_with(framework, emulators, {"offset": {"relative": 0.05}, "noise": {"relative_sd": 0.01}}, "biased")
        biased = run(framework, "biased", sampling(30))
        bias = assess_accuracy([biased], reference_current_a=10, resamples=200)["per_type"]["biased"]["relative_bias"]
        assert bias == pytest.approx(0.05, abs=0.01)
    with allure.step("5 % mains ripple sampled at 7 Hz: readings within ±5 %, visible spread, aliasing to 1 Hz"):
        fixed_with(framework, emulators, {"mains_ripple": {"amplitude_relative": 0.05, "frequency_hz": 50}}, "ripple")
        rippled = run(framework, "ripple", sampling(20, 7))
        assert all(9.5 - 1e-9 <= v <= 10.5 + 1e-9 for v in rippled["values"])
        assert statistics.stdev(rippled["values"]) > 0.01 and rippled["aliasing"]["alias_frequency_hz"] == 1.0
