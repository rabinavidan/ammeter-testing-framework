import math
import random

import allure
import pytest

from helpers import attach_json
from src.analysis.result_analysis import (
    DEFAULT_METRICS,
    alias_frequency,
    analyze_measurements,
    control_chart_analysis,
    control_limits_for,
    trend_test,
    validate_metrics,
)


def noisy(n=40, level=10.0, sd=1.0, seed=0):
    rng = random.Random(seed)
    return [rng.gauss(level, sd) for _ in range(n)]


def _part_metrics_are_correct_and_degrade_gracefully():
    """The spec's statistics match hand-computed values; uncomputable ones are None; selection works."""
    with allure.step("[1, 2, 3, 4, 10]: mean 4, median 3, sample std dev √(50/4), min 1, max 10, range 9, CV"):
        std_dev = math.sqrt((9 + 4 + 1 + 0 + 36) / 4)
        assert analyze_measurements([1.0, 2.0, 3.0, 4.0, 10.0], expected_count=5) == pytest.approx({
            "count": 5, "mean": 4.0, "median": 3.0, "std_dev": std_dev, "min": 1.0, "max": 10.0,
            "range": 9.0, "coefficient_of_variation": std_dev / 4.0, "success_rate": 1.0})
    with allure.step("Even count [2, 4, 4, 4, 5, 5, 7, 9]: median 4.5 (mean of the middle two, not 4 or 5)"):
        assert analyze_measurements([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])["median"] == 4.5
    with allure.step("No readings: every metric None, success rate 0"):
        empty = analyze_measurements([], expected_count=5)
        assert empty["success_rate"] == 0 and all(empty[m] is None for m in DEFAULT_METRICS)
    with allure.step("One reading: std dev and CV None; zero mean: CV None"):
        single, zero = analyze_measurements([3.0]), analyze_measurements([-1.0, 1.0])
        assert single["std_dev"] is None and single["coefficient_of_variation"] is None
        assert zero["coefficient_of_variation"] is None
    with allure.step("Only the selected metrics are computed; [] selects all; an unknown name is rejected"):
        assert analyze_measurements([1.0, 2.0, 4.0], ["mean", "max"], 4) == pytest.approx(
            {"count": 3, "mean": 7 / 3, "max": 4.0, "success_rate": 0.75})
        assert validate_metrics(None) == validate_metrics([]) == DEFAULT_METRICS
        with pytest.raises(ValueError, match="mode"):
            validate_metrics(["mean", "mode"])


def _part_control_chart_rules():
    """Each rule fires on what it is for and not on what it isn't: spikes, level shifts, drift, stable data."""
    with allure.step("Stable noise (40 × 10 A ± 1): in control, limits around the mean"):
        stable = control_chart_analysis(noisy())
        assert stable["in_control"] and stable["lcl"] < stable["center"] < stable["ucl"]
    with allure.step("One ×10 spike at #30: beyond limits only — no fake run (median rule), no fake trend"):
        values = noisy()
        values[30] *= 10
        spiked = control_chart_analysis(values)
        assert spiked["beyond_limits"] == [30] and spiked["runs"] == [] and not spiked["trend"]["significant"]
    with allure.step("Level shift 10 A → 12 A halfway: a run below then a run above the median"):
        shifted = control_chart_analysis(noisy(20, 10.0, 0.3, 1) + noisy(20, 12.0, 0.3, 2))
        assert {"start": 0, "end": 19, "side": "below"} in shifted["runs"] and not shifted["in_control"]
    with allure.step("Runs of 8 (below the 9-reading rule) are not flagged"):
        assert control_chart_analysis(([11.0] * 8 + [9.0] * 8) * 3)["runs"] == []
    with allure.step("A +0.3 A/reading drift: significant Mann-Kendall trend, Sen's slope ≈ 0.3"):
        drift = control_chart_analysis([v * (1 + 0.03 * i) for i, v in enumerate(noisy(sd=0.5))])["trend"]
        assert drift["significant"] and drift["sen_slope_per_reading"] == pytest.approx(0.3, rel=0.15)
    with allure.step("Perfect trend 0..19: S = 190, Sen's slope 1, p < 1e-6"):
        perfect = trend_test([float(i) for i in range(20)])
        assert perfect["s"] == 190 and perfect["sen_slope_per_reading"] == 1.0 and perfect["p_value"] < 1e-6
    with allure.step("Identical readings: in control, sigma 0, trend p 1; <2 readings: None; <10: no trend"):
        flat = control_chart_analysis([5.0] * 20)
        assert flat["in_control"] and flat["sigma"] == 0 and flat["trend"]["p_value"] == 1.0
        assert control_chart_analysis([1.0]) is None and control_chart_analysis([1.0, 2.0, 3.0])["trend"] is None
    with allure.step("Up to 40 readings the classic rules apply (3 sigma, runs of 9); longer runs widen both "
                     "(2,000 readings: about 4.04 sigma, runs of 15) so false alarms don't grow with n"):
        assert control_limits_for(10) == control_limits_for(40) == {"sigma_limit": 3.0, "run_length": 9}
        long_rules = control_limits_for(2000)
        assert long_rules["sigma_limit"] == pytest.approx(4.04, abs=0.01) and long_rules["run_length"] == 15
    with allure.step("2,000 stable readings: in control (the fixed rules flagged 98 % of such runs)"):
        long_stable = control_chart_analysis(noisy(n=2000, seed=3))
        assert long_stable["in_control"] and long_stable["run_length"] == 15
        fixed = control_chart_analysis(noisy(n=2000, seed=3), sigma_limit=3.0, run_length=9)
        assert not fixed["in_control"]
    with allure.step("...but a slow 0.05 %/reading drift over those 2,000 readings is still flagged"):
        slow_drift = control_chart_analysis([v * (1 + 0.0005 * i) for i, v in enumerate(noisy(n=2000, seed=3))])
        assert not slow_drift["in_control"]


def _part_aliasing():
    """Sampled signals fold into 0..fs/2, and aliased mains ripple can look like a drift."""
    cases = [(50, 5, 0), (50, 7, 1), (50, 20, 10), (50, 49.5, 0.5), (60, 50, 10), (50, 200, 50)]
    for signal_hz, sample_hz, alias_hz in cases:
        with allure.step(f"{signal_hz} Hz sampled at {sample_hz} Hz appears at {alias_hz} Hz"):
            assert alias_frequency(signal_hz, sample_hz) == pytest.approx(alias_hz)
    with allure.step("5 % ripple at 50 Hz sampled at 49.5 Hz: the trend test reports a 'drift' that isn't there"):
        values = [10 * (1 + 0.05 * math.sin(2 * math.pi * 50 * i / 49.5 - math.pi / 2)) for i in range(40)]
        control = control_chart_analysis(values)
        attach_json("control chart", control)
        assert control["trend"]["significant"]


@pytest.mark.sanity
def test_statistics_control_chart_and_aliasing():
    """The spec's statistics, the control chart rules and the aliasing arithmetic, against hand-computed values."""
    with allure.step("The spec's statistics match hand-computed values; uncomputable ones are None; selection works."):
        _part_metrics_are_correct_and_degrade_gracefully()
    with allure.step(
            "Each rule fires on what it is for and not on what it isn't: spikes, level shifts, drift, stable "
            "data."):
        _part_control_chart_rules()
    with allure.step("Sampled signals fold into 0..fs/2, and aliased mains ripple can look like a drift."):
        _part_aliasing()
