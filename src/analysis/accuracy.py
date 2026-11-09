import itertools
import math
import random
import statistics
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Scales the median absolute deviation to estimate the standard deviation of normal data
_MAD_TO_SD = 1.4826
# Below this many readings per type the percentile-bootstrap CI of robust CV under-covers (measured: about 89 %
# for a nominal 95 % at 10 readings of the ENTES formula, 96-97 % at 40), so the assessment adds a note
SMALL_SAMPLE = 30


def robust_cv(values: Sequence[float]) -> Optional[float]:
    """
    Scaled MAD / |median|: a coefficient of variation that one extreme reading can't
    dominate (Greenlee's V / R readings spike whenever R lands near its minimum).
    """
    if len(values) < 2:
        return None
    median = statistics.median(values)
    if median == 0:
        return None
    mad = statistics.median(abs(v - median) for v in values)
    return _MAD_TO_SD * mad / abs(median)


def coefficient_of_variation(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    return statistics.stdev(values) / abs(mean) if mean else None


def _median_ratio(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    median_b = statistics.median(b)
    return statistics.median(a) / median_b if median_b else None


def bootstrap_ci(groups: Sequence[Sequence[float]], statistic: Callable[..., Optional[float]],
                 resamples: int, confidence: float, rng: random.Random) -> Optional[Tuple[float, float]]:
    """
    Percentile bootstrap confidence interval for `statistic(*groups)`.

    Each group is resampled independently with replacement. Makes no assumption
    about the readings' distribution. Returns None if the statistic was undefined
    in more than 10% of resamples (too little data to trust the interval).
    """
    estimates = []
    for _ in range(resamples):
        value = statistic(*(rng.choices(group, k=len(group)) for group in groups))
        if value is not None and math.isfinite(value):
            estimates.append(value)
    if len(estimates) < 0.9 * resamples:
        return None
    estimates.sort()
    tail = (1 - confidence) / 2
    low = estimates[int(tail * len(estimates))]
    high = estimates[min(len(estimates) - 1, int((1 - tail) * len(estimates)))]
    return low, high


def validate_accuracy_settings(reference_current_a: Optional[float] = None, confidence: float = 0.95,
                               resamples: int = 2000, seed: Optional[int] = 0) -> None:
    """ValueError naming the setting if an accuracy-assessment parameter is invalid (checked before any sampling)."""
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    if not (number(confidence) and 0 < confidence < 1):
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    if not (isinstance(resamples, int) and not isinstance(resamples, bool) and resamples >= 100):
        raise ValueError(f"resamples must be a whole number of at least 100, got {resamples!r}")
    if reference_current_a is not None and not (number(reference_current_a) and reference_current_a != 0):
        raise ValueError(f"reference_current_a must be a non-zero number or null (relative error divides by it), "
                         f"got {reference_current_a!r}")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ValueError(f"random_seed must be a whole number or null, got {seed!r}")


def assess_accuracy(results: Sequence[Dict], reference_current_a: Optional[float] = None,
                    confidence: float = 0.95, resamples: int = 2000, seed: Optional[int] = 0) -> Dict:
    """
    Compare ammeter types using the readings from one or more runs of each.

    Readings are pooled per ammeter type. For each type: mean and CV with bootstrap
    confidence intervals, robust CV, success rate, and (with 2+ runs) the
    between-run CV of run means. Every pair of types gets the ratio of their medians
    with a confidence interval; the pair agrees if the interval contains 1.

    Relative spread (robust CV) is always assessed. With real meters measuring one steady current it is their
    repeatability; with the formula emulators every reading is a different random current, so it measures how
    widely each emulator's formula spreads. Accuracy (closeness to the true
    current) needs `reference_current_a`: with it, each type also gets bias and
    relative RMSE, and the ranking is by relative RMSE. Without it, the ranking is by
    robust CV, i.e. the most *precise* type, which is not necessarily the most accurate.

    The ranking has a clear winner only if the top type's confidence interval
    doesn't overlap the runner-up's.
    """
    validate_accuracy_settings(reference_current_a, confidence, resamples, seed)

    rng = random.Random(seed)
    notes = []

    runs_by_type: Dict[str, List[Dict]] = {}
    for result in results:
        runs_by_type.setdefault(result["ammeter_type"], []).append(result)

    per_type = {}
    for ammeter_type, runs in runs_by_type.items():
        values = [v for run in runs for v in run.get("values") or []]
        attempted = sum(run["sampling"]["measurements_count"] for run in runs)
        if len(values) < 2:
            notes.append(f"{ammeter_type}: left out, needs at least 2 readings (has {len(values)})")
            continue

        run_means = [statistics.fmean(run["values"]) for run in runs if run.get("values")]
        entry = {
            "runs": len(runs),
            "test_ids": [run["test_id"] for run in runs],
            "readings": len(values),
            "success_rate": len(values) / attempted if attempted else None,
            "mean": statistics.fmean(values),
            "mean_ci": bootstrap_ci([values], statistics.fmean, resamples, confidence, rng),
            "median": statistics.median(values),
            "cv": coefficient_of_variation(values),
            "cv_ci": bootstrap_ci([values], coefficient_of_variation, resamples, confidence, rng),
            "robust_cv": robust_cv(values),
            "robust_cv_ci": bootstrap_ci([values], robust_cv, resamples, confidence, rng),
            "between_run_cv": coefficient_of_variation(run_means) if len(run_means) >= 2 else None,
            # GUM Type A: standard uncertainty of the mean from the readings' scatter, and expanded (k = 2)
            "standard_uncertainty_a": statistics.stdev(values) / math.sqrt(len(values)),
            "expanded_uncertainty_k2_a": 2 * statistics.stdev(values) / math.sqrt(len(values)),
        }

        if reference_current_a is not None:
            ref = reference_current_a

            def relative_rmse(sample, ref=ref):
                return math.sqrt(statistics.fmean((v - ref) ** 2 for v in sample)) / abs(ref)

            entry["bias"] = entry["mean"] - ref
            entry["relative_bias"] = entry["bias"] / ref
            entry["relative_rmse"] = relative_rmse(values)
            entry["relative_rmse_ci"] = bootstrap_ci([values], relative_rmse, resamples, confidence, rng)
        per_type[ammeter_type] = entry

    agreement = []
    for a, b in itertools.combinations(sorted(per_type), 2):
        values_a = [v for run in runs_by_type[a] for v in run.get("values") or []]
        values_b = [v for run in runs_by_type[b] for v in run.get("values") or []]
        ratio = _median_ratio(values_a, values_b)
        ci = bootstrap_ci([values_a, values_b], _median_ratio, resamples, confidence, rng)
        agreement.append({
            "a": a,
            "b": b,
            "median_ratio": ratio,
            "median_ratio_ci": ci,
            "agree": ci is not None and ci[0] <= 1 <= ci[1],
        })

    ranking_metric = "relative_rmse" if reference_current_a is not None else "robust_cv"
    ranked = sorted((t for t in per_type if per_type[t][ranking_metric] is not None),
                    key=lambda t: per_type[t][ranking_metric])
    clear_winner = False
    if len(ranked) >= 2:
        best_ci = per_type[ranked[0]][f"{ranking_metric}_ci"]
        second_ci = per_type[ranked[1]][f"{ranking_metric}_ci"]
        clear_winner = best_ci is not None and second_ci is not None and best_ci[1] < second_ci[0]
    if len(per_type) < 2:
        notes.append("Need readings from at least 2 ammeter types to compare them")
    fewest = min((entry["readings"] for entry in per_type.values()), default=None)
    if fewest is not None and fewest < SMALL_SAMPLE:
        notes.append(f"Only {fewest} readings for some type: below {SMALL_SAMPLE} the bootstrap intervals of robust "
                     f"CV are too narrow (about 89 % coverage for a nominal 95 % at 10 readings), so treat the "
                     f"ranking as indicative only.")
    if reference_current_a is None:
        notes.append("No reference current set: this ranks relative spread (robust CV), not accuracy. With meters "
                     "measuring one steady current that is their repeatability; the emulators draw a new random "
                     "current per reading, so here it compares how widely each one's readings spread. Set "
                     "analysis.accuracy.reference_current_a to a known true current to assess accuracy.")

    return {
        "reference_current_a": reference_current_a,
        "confidence": confidence,
        "resamples": resamples,
        "seed": seed,
        "per_type": per_type,
        "agreement": agreement,
        "ranking_metric": ranking_metric,
        "ranking": ranked,
        "most_reliable": ranked[0] if ranked else None,
        "clear_winner": clear_winner,
        "notes": notes,
    }


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}g}"


def _fmt_ci(ci: Optional[Tuple[float, float]], digits: int = 3) -> str:
    return "n/a" if ci is None else f"[{ci[0]:.{digits}g}, {ci[1]:.{digits}g}]"


def format_accuracy_report(assessment: Dict) -> str:
    """Human-readable summary of `assess_accuracy` output."""
    level = f"{assessment['confidence']:.0%} CI"
    lines = [f"Accuracy assessment ({level}, {assessment['resamples']} bootstrap resamples)"]
    if assessment["reference_current_a"] is not None:
        lines.append(f"Reference current: {assessment['reference_current_a']:g} A")

    for ammeter_type, s in assessment["per_type"].items():
        lines.append(f"\n  {ammeter_type.upper()}  ({s['runs']} run(s), {s['readings']} readings, "
                     f"success {_fmt(s['success_rate'], 3)})")
        # Plain ASCII "+/-": this report is printed, and a console that can't encode U+00B1 would crash on it
        lines.append(f"    mean {_fmt(s['mean'])} A {_fmt_ci(s['mean_ci'])}   median {_fmt(s['median'])} A   "
                     f"U(k=2) +/-{_fmt(s['expanded_uncertainty_k2_a'], 3)} A")
        lines.append(f"    CV {_fmt(s['cv'], 3)} {_fmt_ci(s['cv_ci'])}   "
                     f"robust CV {_fmt(s['robust_cv'], 3)} {_fmt_ci(s['robust_cv_ci'])}"
                     + (f"   between-run CV {_fmt(s['between_run_cv'], 3)}" if s["between_run_cv"] is not None else ""))
        if "bias" in s:
            lines.append(f"    bias {_fmt(s['bias'])} A ({s['relative_bias']:+.1%})   "
                         f"relative RMSE {_fmt(s['relative_rmse'], 3)} {_fmt_ci(s['relative_rmse_ci'])}")

    if assessment["agreement"]:
        lines.append("\n  Agreement between types (ratio of medians; agree = CI contains 1)")
        for pair in assessment["agreement"]:
            lines.append(f"    {pair['a']} / {pair['b']}: {_fmt(pair['median_ratio'], 3)}x "
                         f"{_fmt_ci(pair['median_ratio_ci'])}  -> {'agree' if pair['agree'] else 'DISAGREE'}")

    if assessment["ranking"]:
        metric = assessment["ranking_metric"].replace("_", " ")
        lines.append(f"\n  Ranking by {metric} (lower is better): "
                     + " > ".join(t.upper() for t in assessment["ranking"]))
        if assessment["clear_winner"]:
            lines.append(f"  Most reliable: {assessment['most_reliable'].upper()} (clear winner: its CI is separate "
                         f"from the runner-up's)")
        else:
            lines.append(f"  Most reliable: not established at this sample size; {assessment['most_reliable'].upper()}"
                         f" leads, but its CI overlaps the runner-up's (collect more readings per type)")

    for note in assessment["notes"]:
        lines.append(f"\n  Note: {note}")
    return "\n".join(lines)
