import math

import allure
import pytest

from helpers import FAST, FixedAmmeter, add_ammeter, free_port, run


@pytest.mark.timing
def test_readings_over_a_real_socket_keep_to_the_schedule(framework, fixed_ammeter):
    """On a real socket, readings are taken within 50 ms of i / frequency and the run lasts the whole window."""
    result = run(framework, "fixed", {"measurements_count": 10, "sampling_frequency_hz": 20,
                                      "total_duration_seconds": None})
    with allure.step("10 readings of 10.0 A, no errors, duration derived as 0.5 s"):
        assert result["values"] == [10.0] * 10 and result["errors"] == []
        assert result["sampling"]["total_duration_seconds"] == 0.5
    with allure.step("Every reading within 50 ms after its slot; the run lasted 0.5-0.7 s"):
        offsets = [m["elapsed_s"] - m["index"] / 20 for m in result["measurements"]]
        assert all(0 <= offset < 0.05 for offset in offsets)
        assert 0.5 <= result["actual_duration_seconds"] < 0.7


@pytest.mark.bugfix
def test_bad_devices_become_recorded_errors(framework, emulators):
    """Regression (NaN/inf): over real sockets, unreachable, non-finite and implausible devices give errors."""
    with allure.step("An unreachable device: 3 attempts, each refused or timed out; success rate 0"):
        add_ammeter(framework, "dead", free_port())
        dead = run(framework, "dead", {"measurements_count": 3, "sampling_frequency_hz": 20,
                                       "total_duration_seconds": None})
        assert dead["values"] == [] and len(dead["errors"]) == 3
        assert set(dead["error_counts"]) <= {"ConnectionRefusedError", "TimeoutError"}
    for value in (math.nan, math.inf):
        with allure.step(f"A device replying {value}: all 5 readings rejected as non-finite"):
            port = emulators(FixedAmmeter(free_port(), value=value))
            add_ammeter(framework, f"broken_{value}", port)
            broken = run(framework, f"broken_{value}", FAST)
            assert broken["values"] == [] and broken["error_counts"] == {"ValueError": 5}
    with allure.step("A device reading 500 A with plausible range 5..200: 5 ImplausibleReadingError"):
        port = emulators(FixedAmmeter(free_port(), value=500.0))
        add_ammeter(framework, "entes_like", port, plausible_range_a=[5, 200])
        implausible = run(framework, "entes_like", FAST)
        assert implausible["values"] == [] and implausible["error_counts"] == {"ImplausibleReadingError": 5}
