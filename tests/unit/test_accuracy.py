import random
import statistics

import allure
import pytest

from helpers import attach_json, make_result
from src.analysis.accuracy import assess_accuracy, bootstrap_ci, format_accuracy_report, robust_cv

rng = random.Random(1)
ON_TARGET_PRECISE = [rng.gauss(10, 0.1) for _ in range(200)]
ON_TARGET_NOISY = [rng.gauss(10, 1.0) for _ in range(200)]
BIASED_PRECISE = [rng.gauss(20, 0.1) for _ in range(200)]      # most precise relative to size, but reads x2
DATA = [make_result("a", ON_TARGET_PRECISE), make_result("b", ON_TARGET_NOISY), make_result("c", BIASED_PRECISE)]


def _part_precision_accuracy_and_agreement():
    """Without a reference the most precise type wins (and the report says so); with one, the most accurate."""
    precision = assess_accuracy(DATA, resamples=500)
    attach_json("assessment", precision)
    with allure.step("No reference: ranked by robust CV, c > a > b, a clear winner, the note says it is not accuracy"):
        assert precision["ranking_metric"] == "robust_cv" and precision["ranking"] == ["c", "a", "b"]
        assert precision["clear_winner"] and any("not accuracy" in n for n in precision["notes"])
    with allure.step("Reference 10 A: ranked by relative RMSE, a > b > c; c's bias is +100 %"):
        accuracy = assess_accuracy(DATA, reference_current_a=10, resamples=500)
        assert accuracy["ranking"] == ["a", "b", "c"]
        assert accuracy["per_type"]["c"]["relative_bias"] == pytest.approx(1.0, abs=0.01)
    with allure.step("Agreement by median-ratio interval: a/b agree (both 10 A); a/c disagree (ratio ≈ 0.5)"):
        pairs = {(p["a"], p["b"]): p for p in precision["agreement"]}
        assert pairs[("a", "b")]["agree"] and not pairs[("a", "c")]["agree"]
        assert pairs[("a", "c")]["median_ratio"] == pytest.approx(0.5, rel=0.01)
    with allure.step("GUM Type A: u = std/√n, U(k=2) = 2u; the text report names every type and the verdict"):
        expected = statistics.stdev(ON_TARGET_NOISY) / len(ON_TARGET_NOISY) ** 0.5
        assert precision["per_type"]["b"]["standard_uncertainty_a"] == pytest.approx(expected)
        assert precision["per_type"]["b"]["expanded_uncertainty_k2_a"] == pytest.approx(2 * expected)
        report = format_accuracy_report(precision)
        assert all(text in report for text in ("A ", "B ", "C ", "Most reliable: C", "Note:"))
    with allure.step("When the CIs overlap, the report says no winner is established instead of naming one"):
        report = format_accuracy_report({**precision, "clear_winner": False})
        assert "Most reliable: not established at this sample size; C leads" in report
    with allure.step("The printed report is plain ASCII (U(k=2) +/-), so an ASCII or cp1252 console can't crash on it"):
        assert format_accuracy_report(precision).isascii() and "U(k=2) +/-" in report
    with allure.step("200 readings per type: no small-sample note; 10 per type: the ranking is flagged as indicative"):
        assert not any("indicative" in n for n in precision["notes"])
        small = assess_accuracy([make_result(t, values[:10]) for t, values in
                                 (("a", ON_TARGET_PRECISE), ("b", ON_TARGET_NOISY))], resamples=200)
        assert any("Only 10 readings" in n and "indicative" in n for n in small["notes"])


def _part_statistical_techniques_and_edge_cases():
    """Robust CV resists outliers; seeded results repeat; thin data and bad settings are handled."""
    cv = statistics.stdev(ON_TARGET_NOISY) / statistics.fmean(ON_TARGET_NOISY)
    with allure.step(f"Robust CV ≈ CV on normal data ({cv:.4f}); one 10,000 A outlier barely moves it"):
        assert robust_cv(ON_TARGET_NOISY) == pytest.approx(cv, rel=0.15)
        assert robust_cv(ON_TARGET_NOISY + [10_000]) == pytest.approx(robust_cv(ON_TARGET_NOISY), rel=0.02)
    with allure.step("The same data with the fixed seed gives the same assessment"):
        assert assess_accuracy(DATA, resamples=200) == assess_accuracy(DATA, resamples=200)
    with allure.step("A type with 1 reading is left out with a note; no data -> no verdict"):
        thin = assess_accuracy([make_result("a", ON_TARGET_PRECISE), make_result("b", [5.0])], resamples=200)
        assert list(thin["per_type"]) == ["a"] and any("b: left out" in n for n in thin["notes"])
        assert assess_accuracy([], resamples=200)["most_reliable"] is None
    with allure.step("Between-run CV needs 2 runs of a type"):
        runs = [make_result("a", ON_TARGET_PRECISE[:15], "a_1"), make_result("a", ON_TARGET_PRECISE[15:30], "a_2"),
                make_result("b", ON_TARGET_NOISY)]
        two = assess_accuracy(runs, resamples=200)["per_type"]
        assert two["a"]["between_run_cv"] is not None and two["b"]["between_run_cv"] is None
    for kwargs, message in [({"reference_current_a": 0}, "non-zero"), ({"confidence": 1.5}, "between 0 and 1"),
                            ({"resamples": 10}, "at least 100"), ({"confidence": "high"}, "between 0 and 1"),
                            ({"resamples": 500.5}, "whole number"), ({"seed": "abc"}, "random_seed")]:
        with allure.step(f"{kwargs} -> ValueError '{message}'"):
            with pytest.raises(ValueError, match=message):
                assess_accuracy(DATA, **kwargs)


def test_the_accuracy_assessment():
    """Precision vs accuracy ranking, agreement between types, GUM uncertainty, robust statistics and edge cases."""
    with allure.step(
            "Without a reference the most precise type wins (and the report says so); with one, the most "
            "accurate."):
        _part_precision_accuracy_and_agreement()
    with allure.step("Robust CV resists outliers; seeded results repeat; thin data and bad settings are handled."):
        _part_statistical_techniques_and_edge_cases()


@pytest.mark.slow
def test_95_percent_intervals_contain_the_true_mean_about_95_percent_of_the_time():
    """The bootstrap's 95 % intervals really contain the true value about 95 % of the time (n = 30)."""
    trial_rng = random.Random(7)
    hits = 0
    for _ in range(200):
        low, high = bootstrap_ci([[trial_rng.gauss(10, 1) for _ in range(30)]], statistics.fmean, 300, 0.95,
                                 trial_rng)
        hits += low <= 10 <= high
    with allure.step(f"{hits}/200 = {hits / 200:.1%} contain 10 A; expect 88-99 % (percentile bootstrap)"):
        assert 0.88 <= hits / 200 <= 0.99
