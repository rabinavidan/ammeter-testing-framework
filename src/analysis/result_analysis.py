import math
import statistics
from collections import Counter
from statistics import NormalDist
from typing import Any, Callable, Dict, List, Optional, Sequence


def _std_dev(values: Sequence[float]) -> Optional[float]:
    # Sample standard deviation (n - 1): the readings are a sample of the ammeter's output
    return statistics.stdev(values) if len(values) >= 2 else None


def _coefficient_of_variation(values: Sequence[float]) -> Optional[float]:
    # std_dev / |mean|: spread relative to the size of the reading, comparable across ammeters
    std_dev = _std_dev(values)
    mean = statistics.fmean(values)
    if std_dev is None or mean == 0:
        return None
    return std_dev / abs(mean)


# Each metric takes a non-empty list of values and returns a float, or None if it can't be computed
METRICS: Dict[str, Callable[[Sequence[float]], Optional[float]]] = {
    "mean": statistics.fmean,
    "median": statistics.median,
    "std_dev": _std_dev,
    "min": min,
    "max": max,
    "range": lambda values: max(values) - min(values),
    "coefficient_of_variation": _coefficient_of_variation,
}

DEFAULT_METRICS = list(METRICS)


def alias_frequency(signal_hz: float, sample_hz: float) -> float:
    """
    The frequency a signal appears at when sampled at `sample_hz`: it folds into 0..sample_hz/2.
    For example 50 Hz mains sampled at 7 Hz shows up as a 1 Hz wander; at 5 Hz (or any whole
    divisor of 50) as 0 Hz, a constant offset that depends on where in the cycle sampling began.
    """
    if signal_hz < 0 or sample_hz <= 0:
        raise ValueError("need signal_hz >= 0 and sample_hz > 0")
    folded = math.fmod(signal_hz, sample_hz)
    alias = min(folded, sample_hz - folded)
    return round(alias, 9)      # 50 % 0.1 leaves floating-point dust


def validate_metrics(metrics: Optional[List[str]]) -> List[str]:
    """Return the metric names to compute; None or empty means all of them."""
    if not metrics:
        return DEFAULT_METRICS
    unknown = [m for m in metrics if m not in METRICS]
    if unknown:
        raise ValueError(f"Unknown statistical metrics {unknown}. Available: {DEFAULT_METRICS}")
    return list(metrics)


# d2 constant: sigma ≈ mean moving range / 1.128 for moving ranges of 2 consecutive readings
_D2 = 1.128
# Nelson rule 2 (Western Electric uses 8, which gave about twice the false alarms on CIRCUTOR data)
RUN_LENGTH = 9
# Mann-Kendall's normal approximation is reliable from about 10 readings
_MIN_TREND_READINGS = 10


def trend_test(values: Sequence[float], alpha: float = 0.01) -> Optional[Dict]:
    """
    Mann-Kendall test for a monotonic trend (drift) in readings taken in order.

    Counts, over every pair of readings, whether the later one is higher or lower
    (S), and compares S with what random order would give. Rank-based, so a spike
    counts as one reading rather than dragging a fitted line. The size of the trend
    is Sen's slope: the median slope over all pairs of readings, as a fraction of
    the median reading. O(n^2), fine for a few thousand readings.

    Returns None for fewer than 10 readings.
    """
    n = len(values)
    if n < _MIN_TREND_READINGS:
        return None
    s = 0
    slopes = []
    for i in range(n):
        for j in range(i + 1, n):
            s += (values[j] > values[i]) - (values[j] < values[i])
            slopes.append((values[j] - values[i]) / (j - i))
    # Variance of S under no trend, corrected for groups of tied readings
    ties = sum(t * (t - 1) * (2 * t + 5) for t in Counter(values).values() if t > 1)
    variance = (n * (n - 1) * (2 * n + 5) - ties) / 18
    if variance == 0:     # every reading identical
        z, p_value = 0.0, 1.0
    else:
        z = (s - (s > 0) + (s < 0)) / math.sqrt(variance)     # continuity correction
        p_value = 2 * (1 - NormalDist().cdf(abs(z)))
    sen_slope = statistics.median(slopes)
    median = statistics.median(values)
    return {
        "method": "mann_kendall",
        "s": s,
        "z": z,
        "p_value": p_value,
        "alpha": alpha,
        "significant": p_value < alpha,
        "sen_slope_per_reading": sen_slope,
        "relative_slope_per_reading": sen_slope / abs(median) if median else None,
        "sen_intercept": statistics.median(v - sen_slope * i for i, v in enumerate(values)),
    }


# The classic rules (3 sigma, runs of 9) were measured on 40-reading runs; that is where they apply unchanged.
REFERENCE_READINGS = 40
_SIGMA_LIMIT = 3.0


def control_limits_for(n: int) -> Dict[str, Any]:
    """
    The sigma limit and run length for a run of n readings. Up to REFERENCE_READINGS readings they are the classic
    3 sigma and 9; for longer runs both grow so that each rule's expected number of false alarms on a stable
    process stays at its level for 40 readings, instead of growing with n:
      - limits (Bonferroni): each reading may cross with probability budget / n, where budget = 40 x P(|z| > 3);
      - runs: the expected number of chance runs of length L is about n / 2^L, so L grows by log2(n / 40).
    Measured on stable CIRCUTOR readings: false alarms 1 % / 6 % / 28 % / 98 % at 10 / 40 / 200 / 2,000 readings
    with the fixed rules, 1 % / 6 % / 6 % / 7 % with these.
    """
    if n <= REFERENCE_READINGS:
        return {"sigma_limit": _SIGMA_LIMIT, "run_length": RUN_LENGTH}
    budget = REFERENCE_READINGS * 2 * (1 - NormalDist().cdf(_SIGMA_LIMIT))
    sigma_limit = NormalDist().inv_cdf(1 - budget / (2 * n))
    run_length = RUN_LENGTH + math.ceil(math.log2(n / REFERENCE_READINGS))
    return {"sigma_limit": sigma_limit, "run_length": run_length}


def control_chart_analysis(values: Sequence[float], sigma_limit: Optional[float] = None,
                           run_length: Optional[int] = None) -> Optional[Dict]:
    """
    Individuals (Shewhart) control chart for readings in time order. Three rules
    flag an unstable process:

      - beyond_limits: a reading outside mean ± sigma_limit * sigma, with sigma from
        the mean moving range (mean |x[i] - x[i-1]| / 1.128), so a slow drift doesn't
        widen the limits the way the overall standard deviation would (spikes)
      - runs: run_length or more consecutive readings on the same side of the
        *median* (a shifted offset). The median, unlike the mean, isn't pulled up by
        a spike, which would leave most readings below the mean and fake a run.
        Readings exactly on the median break a run.
      - trend: a significant Mann-Kendall trend (drift); see trend_test

    sigma_limit and run_length default to control_limits_for(len(values)): the classic 3 sigma and 9 up to 40
    readings, widening for longer runs so stable data isn't flagged just for being long. Pass numbers to fix them.

    These choices were measured on 40-reading runs of the real emulator formulas.
    False alarms on clean data: CIRCUTOR ~4%, ENTES ~12% (its readings are skewed, so
    the upper limit is crossed more often). Detection: 5% of readings spiked x6 ~80%;
    a 5%-per-reading drift ~85%, a 3% drift ~50%; a +60% level shift halfway ~50%. Greenlee's
    V / R readings are so heavy-tailed that they almost always cross the limits: its output is
    not a stable process.

    Returns None for fewer than 2 readings. Indexes refer to positions in `values`.
    """
    if len(values) < 2:
        return None
    adaptive = control_limits_for(len(values))
    limit: float = adaptive["sigma_limit"] if sigma_limit is None else sigma_limit
    min_run: int = adaptive["run_length"] if run_length is None else run_length
    center = statistics.fmean(values)
    mean_moving_range = statistics.fmean(abs(b - a) for a, b in zip(values, values[1:], strict=False))
    sigma = mean_moving_range / _D2
    ucl = center + limit * sigma
    lcl = center - limit * sigma

    beyond_limits = [i for i, v in enumerate(values) if v > ucl or v < lcl]

    median = statistics.median(values)
    runs = []
    run_start, run_side = 0, 0
    for i, v in enumerate(list(values) + [median]):     # sentinel closes the last run
        side = (v > median) - (v < median)
        if side != run_side or side == 0:
            if run_side != 0 and i - run_start >= min_run:
                runs.append({"start": run_start, "end": i - 1, "side": "above" if run_side > 0 else "below"})
            run_start, run_side = i, side

    trend = trend_test(values)
    return {
        "center": center,
        "median": median,
        "sigma": sigma,
        "sigma_limit": limit,
        "ucl": ucl,
        "lcl": lcl,
        "run_length": min_run,
        "beyond_limits": beyond_limits,
        "runs": runs,
        "trend": trend,
        "in_control": not beyond_limits and not runs and not (trend and trend["significant"]),
    }


def analyze_measurements(values: Sequence[float], metrics: Optional[List[str]] = None,
                         expected_count: Optional[int] = None) -> Dict:
    """
    Compute the requested statistics for a run's readings.

    Metrics that can't be computed (no readings, or std_dev with a single reading) are None.
    `expected_count` is the number of readings attempted; it's used for `success_rate`.
    """
    names = validate_metrics(metrics)
    analysis: Dict[str, Optional[float]] = {"count": len(values)}
    for name in names:
        analysis[name] = METRICS[name](values) if values else None
    if expected_count:
        analysis["success_rate"] = len(values) / expected_count
    return analysis
