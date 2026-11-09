import copy
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from Ammeters.client import request_current_from_ammeter
from Ammeters.error_simulation import ErrorSimulator
from src.analysis.accuracy import assess_accuracy, validate_accuracy_settings
from src.analysis.result_analysis import (
    alias_frequency,
    analyze_measurements,
    control_chart_analysis,
    validate_metrics,
)
from src.analysis.visualization import create_run_plots, plot_comparison, validate_plot_types
from src.results.result_archive import ResultArchive
from src.utils.config import load_config
from src.utils.logger import TestLogger

# Sends one request: (port, command, timeout, host=...) -> reading in amps, or None if the meter sent nothing
Transport = Callable[..., Optional[float]]


class ImplausibleReadingError(ValueError):
    """A reading outside the ammeter's configured plausible_range_a: a fault, not a measurement."""


class NoReplyError(ValueError):
    """The ammeter closed the connection without sending anything (dropped, or the command wasn't recognised)."""


# Ammeter names become part of test IDs, which are file names in the archive
_AMMETER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class RealClock:
    """The wall clock. Tests can pass their own object with the same now()/sleep() to control time."""

    @staticmethod
    def now() -> float:
        return time.perf_counter()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


def deep_merge(base: Dict, overrides: Dict) -> Dict:
    """A copy of `base` with `overrides` merged in; nested dicts merge, everything else replaces."""
    merged = copy.deepcopy(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def resolve_sampling(sampling_config: Dict[str, Any]) -> Tuple[int, float, float]:
    """
    Derive (count, duration, frequency) from any two of the three config values,
    using count = duration * frequency.
    """
    count = sampling_config.get("measurements_count")
    duration = sampling_config.get("total_duration_seconds")
    frequency = sampling_config.get("sampling_frequency_hz")

    for name, value in (("measurements_count", count),
                        ("total_duration_seconds", duration),
                        ("sampling_frequency_hz", frequency)):
        if value is not None and (not _is_number(value) or value <= 0):
            raise ValueError(f"sampling.{name} must be a positive finite number, got {value!r}")
    if count is not None and count != int(count):
        raise ValueError(f"sampling.measurements_count must be a whole number, got {count!r}")

    given = sum(v is not None for v in (count, duration, frequency))
    if given < 2:
        raise ValueError(
            "Set at least two of sampling.measurements_count, "
            "sampling.total_duration_seconds and sampling.sampling_frequency_hz"
        )

    if count is None:
        assert duration is not None and frequency is not None
        count = max(1, round(duration * frequency))
    elif frequency is None:
        assert duration is not None
        frequency = count / duration
    elif duration is None:
        duration = count / frequency
    elif round(duration * frequency) != count:
        raise ValueError(
            f"Inconsistent sampling config: {count} samples != "
            f"{duration} s * {frequency} Hz"
        )

    return int(count), float(duration), float(frequency)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_testing(testing_config: Dict[str, Any]) -> None:
    """Check the testing section's scalar settings; ValueError naming the key if one is invalid."""
    for key in ("request_timeout_seconds", "mains_frequency_hz"):
        value = testing_config.get(key)
        if value is not None and not (_is_number(value) and value > 0):
            raise ValueError(f"testing.{key} must be a positive number or null, got {value!r}")


def validate_ammeter(name: str, ammeter: Dict[str, Any]) -> None:
    """Check one `ammeters.<name>` entry; ValueError naming the ammeter and key if it is invalid."""
    if not isinstance(name, str) or not _AMMETER_NAME.match(name):
        raise ValueError(f"Ammeter name {name!r} may only contain letters, digits, '_' and '-' "
                         f"(it becomes part of each run's test ID and archive file name)")
    host = ammeter.get("host", "localhost")
    if not isinstance(host, str) or not host:
        raise ValueError(f"Ammeter '{name}' host must be a non-empty hostname, got {host!r}")
    for key in ("port", "command"):
        if ammeter.get(key) is None:
            raise ValueError(f"Ammeter '{name}' is missing '{key}' in config")
    port = ammeter["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(f"Ammeter '{name}' port must be a whole number from 1 to 65535, got {port!r}")
    if not isinstance(ammeter["command"], str) or not ammeter["command"]:
        raise ValueError(f"Ammeter '{name}' command must be a non-empty string, got {ammeter['command']!r}")
    plausible = ammeter.get("plausible_range_a")
    if plausible is not None:
        if (not isinstance(plausible, (list, tuple)) or len(plausible) != 2
                or not all(_is_number(v) for v in plausible) or plausible[0] >= plausible[1]):
            raise ValueError(f"Ammeter '{name}' plausible_range_a must be [low, high] with low < high, "
                             f"got {plausible!r}")


def _enabled(section: Dict[str, Any], name: str, default: bool) -> bool:
    """An `enabled` switch; must be a real boolean (the string "false" would otherwise count as on)."""
    value = section.get("enabled", default)
    if not isinstance(value, bool):
        raise ValueError(f"{name}.enabled must be true or false, got {value!r}")
    return value


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check every section the framework uses, so a bad value fails before any request is sent rather than
    after the readings were taken. Returns the derived settings configure() commits; ValueError naming the
    setting otherwise.
    """
    testing_config = config.get("testing") or {}
    validate_testing(testing_config)
    resolve_sampling(testing_config.get("sampling") or {})
    for name, ammeter in (config.get("ammeters") or {}).items():
        validate_ammeter(name, ammeter or {})
    analysis = config.get("analysis") or {}
    validate_metrics(analysis.get("statistical_metrics"))
    chart_rules = analysis.get("control_chart") or {}
    if not isinstance(chart_rules, dict):
        raise ValueError(f"analysis.control_chart must be a mapping, got {chart_rules!r}")
    sigma_limit, run_length = chart_rules.get("sigma_limit"), chart_rules.get("run_length")
    if sigma_limit is not None and not (_is_number(sigma_limit) and sigma_limit > 0):
        raise ValueError(f"analysis.control_chart.sigma_limit must be a positive number or null, got {sigma_limit!r}")
    if run_length is not None and not (isinstance(run_length, int) and not isinstance(run_length, bool)
                                       and run_length >= 2):
        raise ValueError(f"analysis.control_chart.run_length must be a whole number >= 2 or null, got {run_length!r}")
    visualization_config = analysis.get("visualization") or {}
    accuracy_config = analysis.get("accuracy") or {}
    validate_accuracy_settings(accuracy_config.get("reference_current_a"),
                               accuracy_config.get("confidence_level", 0.95),
                               accuracy_config.get("bootstrap_resamples", 2000),
                               accuracy_config.get("random_seed", 0))
    result_config = config.get("result_management") or {}
    simulation = config.get("error_simulation") or {}
    if _enabled(simulation, "error_simulation", False):
        # Builds (and so validates) each ammeter's simulator: fault names, parameters, hang vs timeout
        for ammeter_type in [*(config.get("ammeters") or {}), *(simulation.get("ammeters") or {})] or [""]:
            ErrorSimulator.from_config(config, ammeter_type)
    return {
        "plot_types": validate_plot_types(visualization_config.get("plot_types")),
        "plots_enabled": _enabled(visualization_config, "analysis.visualization", True),
        "plots_dir": os.path.normpath(visualization_config.get("output_dir") or "results/plots"),
        "archive_enabled": _enabled(result_config, "result_management", True),
        "archive_dir": result_config.get("archive_dir") or "results/archive",
        "log_dir": os.path.normpath((config.get("logging") or {}).get("output_dir") or "results/logs"),
    }


@dataclass
class _Run:
    """One run's settings, its log and the warnings so far: what the stages of run_test pass along."""
    test_id: str
    ammeter_type: str
    ammeter: Dict[str, Any]
    count: int
    duration: float
    frequency: float
    timeout: Optional[float]
    metrics: List[str]
    plausible: Optional[Sequence[float]]
    mains_hz: Optional[float]
    logger: TestLogger
    chart_rules: Dict[str, Any] = field(default_factory=dict)     # analysis.control_chart overrides (or none)
    warnings: List[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        # Logged for people, and kept in the result for code (tests, reports) to check
        self.warnings.append(message)
        self.logger.warning(message)


class AmmeterTestFramework:
    def __init__(self, config_path: str = "config/config.yaml", overrides: Optional[Dict] = None,
                 clock: Optional[Any] = None, transport: Optional[Transport] = None):
        """
        `overrides` are merged into the loaded config (see configure). `clock` (now()/sleep()) and
        `transport` (how one reading is requested) default to the real ones; tests can replace them
        to check the sampling logic without sockets or waiting.
        """
        self.config_path = config_path
        self.config = load_config(config_path)
        self.clock = clock or RealClock()
        self.transport = transport or request_current_from_ammeter
        self.configure(overrides or {})

    def configure(self, overrides: Dict) -> None:
        """
        Merge `overrides` into the config and re-derive everything computed from it (archive,
        chart settings). All or nothing: invalid settings raise ValueError and leave the
        framework exactly as it was.
        """
        config = deep_merge(self.config, overrides)
        settings = validate_config(config)
        # Everything validated: commit
        self.config = config
        self.plot_types = settings["plot_types"]
        self.plots_enabled = settings["plots_enabled"]
        self.plots_dir = settings["plots_dir"]
        self.archive_enabled = settings["archive_enabled"]
        self.archive = ResultArchive(settings["archive_dir"])
        self.log_dir = settings["log_dir"]

    def register_ammeter(self, name: str, port: int, command: str,
                         plausible_range_a: Optional[Sequence[float]] = None) -> None:
        """Add (or replace) an ammeter in the config; it is validated like a configured one."""
        entry: Dict[str, Any] = {"port": port, "command": command}
        if plausible_range_a is not None:
            entry["plausible_range_a"] = list(plausible_range_a)
        previous = self.config.get("ammeters")
        self.config["ammeters"] = {**(previous or {}), name: entry}
        try:
            self.ammeter_config(name)
        except ValueError:
            self.config["ammeters"] = previous     # a rejected ammeter leaves the config as it was
            raise

    def plot_comparison(self, results: Sequence[Dict], filename: str = "comparison.png") -> Optional[str]:
        """
        Draw one chart comparing the spread of several runs (results from run_test or
        archive.load). Returns the file path, or None if no run had readings to plot.
        """
        return plot_comparison(results, os.path.join(self.plots_dir, filename), self._ammeter_order())

    def assess_accuracy(self, results: Optional[Sequence[Dict]] = None, save: bool = True) -> Dict:
        """
        Compare ammeter types (see src.analysis.accuracy.assess_accuracy), using the
        settings in analysis.accuracy. `results` are run_test results or archived
        records. By default: the latest archived run, with readings, of each ammeter in
        the config's `ammeters` section (see latest_runs). Pooling the whole archive
        would mix in calibration sweeps at other currents, failed runs and ad-hoc
        ammeters, and compare things that aren't comparable. `runs_used` lists the test
        IDs assessed. With `save`, the assessment is written to
        `<output_dir>/accuracy_<timestamp>_<id>.json` and its path stored under `report_path`.
        """
        accuracy_config = (self.config.get("analysis") or {}).get("accuracy") or {}
        if results is None:
            results = self.latest_runs()

        assessment = assess_accuracy(
            results,
            reference_current_a=accuracy_config.get("reference_current_a"),
            confidence=accuracy_config.get("confidence_level", 0.95),
            resamples=accuracy_config.get("bootstrap_resamples", 2000),
            seed=accuracy_config.get("random_seed", 0),
        )
        assessment["runs_used"] = [r.get("test_id") for r in results]
        assessment["report_path"] = None
        if save:
            output_dir = os.path.normpath(accuracy_config.get("output_dir") or "results/accuracy")
            os.makedirs(output_dir, exist_ok=True)
            path = os.path.join(output_dir, f"accuracy_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.json")
            assessment["report_path"] = path
            with open(path, "x", encoding="utf-8") as f:
                json.dump(assessment, f, indent=2)
        return assessment

    def latest_runs(self) -> List[Dict]:
        """For each ammeter in the config, its most recent archived run that has at least 2 readings."""
        latest: Dict[str, Dict] = {}
        for run in self.archive.list_runs():
            if run["ammeter_type"] not in (self.config.get("ammeters") or {}):
                continue
            current = latest.get(run["ammeter_type"])
            if current is None or run["start_time"] > current["start_time"]:
                record = self.archive.load(run["test_id"])
                if len(record.get("values") or []) >= 2:
                    latest[run["ammeter_type"]] = record
        return [latest[name] for name in self._ammeter_order() if name in latest]

    def _ammeter_order(self) -> List[str]:
        # Chart colors follow each ammeter's position in the config
        return list(self.config.get("ammeters") or {})

    def run_test(self, ammeter_type: str, sampling: Optional[Dict[str, Any]] = None) -> Dict:
        """
        Sample one ammeter according to the config's testing.sampling section.

        `sampling` optionally overrides individual sampling keys for this run.
        Requests are scheduled at fixed offsets (i / frequency) from the start, so
        a slow request doesn't push back every later sample. Failed requests are
        recorded in `errors` and the run continues.

        If visualization is enabled, the configured plot types are saved as PNGs and
        listed in `plots`. If result_management is enabled the result is archived
        under its test ID. A plotting or archiving failure is logged and recorded in
        `plot_error` / `archive_error` (leaving `plots` empty or `archive_path` None),
        but the result is still returned.
        """
        ammeter = self.ammeter_config(ammeter_type)
        testing_config = self.config.get("testing") or {}
        sampling_config = dict(testing_config.get("sampling") or {})
        sampling_config.update(sampling or {})
        count, duration, frequency = resolve_sampling(sampling_config)
        timeout = testing_config.get("request_timeout_seconds")
        metrics = validate_metrics((self.config.get("analysis") or {}).get("statistical_metrics"))

        plausible = ammeter.get("plausible_range_a")
        mains_hz = testing_config.get("mains_frequency_hz", 50)

        test_id = self.new_test_id(ammeter_type)
        run = _Run(test_id, ammeter_type, ammeter, count, duration, frequency, timeout, metrics, plausible, mains_hz,
                   TestLogger(test_id, self.log_dir),
                   dict((self.config.get("analysis") or {}).get("control_chart") or {}))
        try:
            run.logger.info(
                f"Starting test {test_id}: {ammeter_type} on port {ammeter['port']}, "
                f"{count} samples at {frequency:g} Hz over {duration:g} s"
            )
            aliasing = self._check_aliasing(run)
            sampled = self._sample(run)
            result = self._analyse(run, sampled, aliasing)
            self._persist(run, result)
            return result
        finally:
            run.logger.close()      # one log file handle per run; never left open

    @staticmethod
    def _check_aliasing(run: _Run) -> Optional[Dict[str, Any]]:
        """Sampling below twice the mains frequency is recorded and warned about; None if the check is off."""
        if not run.mains_hz:
            return None
        # Sampling below twice the mains frequency folds any mains ripple down to a low "alias"
        # frequency, where it looks like a slow wander, a drift, or (at 0 Hz) a fixed offset
        aliasing = {"mains_frequency_hz": run.mains_hz, "sampling_frequency_hz": run.frequency,
                    "nyquist_ok": run.frequency > 2 * run.mains_hz,
                    "alias_frequency_hz": alias_frequency(run.mains_hz, run.frequency)}
        if not aliasing["nyquist_ok"]:
            run.warn(
                f"Sampling at {run.frequency:g} Hz is below twice the {run.mains_hz:g} Hz mains frequency: any mains "
                f"ripple will appear at {aliasing['alias_frequency_hz']:g} Hz"
                + (" (as a constant offset)" if aliasing["alias_frequency_hz"] == 0 else "")
            )
        return aliasing

    def _read_one(self, run: _Run, command: bytes) -> float:
        """One reading, or the exception that says why there is none."""
        value = self.transport(run.ammeter["port"], command, run.timeout, host=run.ammeter.get("host", "localhost"))
        if value is None:
            raise NoReplyError("ammeter returned no data (connection dropped, or command not recognised?)")
        if not math.isfinite(value):
            # float() accepts "NaN" and "inf"; either would poison every statistic
            raise ValueError(f"ammeter returned a non-finite reading: {value!r}")
        if run.plausible and not run.plausible[0] <= value <= run.plausible[1]:
            raise ImplausibleReadingError(
                f"{value!r} A is outside the plausible range {run.plausible[0]:g}..{run.plausible[1]:g} A")
        return value

    def _sample(self, run: _Run) -> Dict[str, Any]:
        """
        Take the readings on an absolute schedule (reading i at i / frequency from the start) and hold until
        the window has passed. Returns start_time, measurements, errors, actual_duration and timing.
        """
        command = run.ammeter["command"].encode("utf-8")
        interval = 1.0 / run.frequency
        measurements: List[Dict[str, Any]] = []
        errors = []
        start_time = datetime.now()
        start = self.clock.now()
        max_lateness = 0.0

        for index in range(run.count):
            delay = start + index * interval - self.clock.now()
            if delay > 0:
                self.clock.sleep(delay)

            elapsed = self.clock.now() - start
            max_lateness = max(max_lateness, elapsed - index * interval)
            try:
                value = self._read_one(run, command)
            except (OSError, ValueError) as e:
                errors.append({"index": index, "elapsed_s": elapsed, "type": type(e).__name__,
                               "error": f"{type(e).__name__}: {e}"})
                run.logger.error(f"Sample {index} failed: {type(e).__name__}: {e}")
                continue

            measurements.append({
                "index": index,
                "timestamp": datetime.now().isoformat(),
                "elapsed_s": elapsed,
                "value": value,
            })
            run.logger.debug(f"Sample {index} at {elapsed:.4f}s: {value} A")

        # Samples sit at the start of each interval; hold until the full window has elapsed
        remaining = start + run.duration - self.clock.now()
        if remaining > 0:
            self.clock.sleep(remaining)

        actual_duration = self.clock.now() - start
        timing = {"max_lateness_s": max_lateness,
                  "achieved_frequency_hz": run.count / actual_duration if actual_duration > 0 else None}
        if max_lateness > interval / 2 or actual_duration > run.duration * 1.1:
            # The schedule couldn't be kept (requests slower than the interval): the readings are not at the
            # requested times, so frequency-based conclusions (aliasing, drift per second) don't hold
            run.warn(f"Sampling fell behind schedule: requests up to {max_lateness * 1000:.0f} ms late, "
                     f"{actual_duration:.3f} s for a {run.duration:g} s window "
                     f"(achieved {timing['achieved_frequency_hz']:.3g} Hz of {run.frequency:g} Hz requested)")
        run.logger.info(
            f"Finished test {run.test_id}: {len(measurements)}/{run.count} samples ok, "
            f"{len(errors)} errors, {actual_duration:.3f} s"
        )
        return {"start_time": start_time, "measurements": measurements, "errors": errors,
                "actual_duration": actual_duration, "timing": timing}

    @staticmethod
    def _analyse(run: _Run, sampled: Dict[str, Any], aliasing: Optional[Dict[str, Any]]) -> Dict:
        """Statistics and control chart of the readings, assembled into the run's result."""
        measurements = sampled["measurements"]
        errors = sampled["errors"]
        values: List[float] = [m["value"] for m in measurements]
        analysis = analyze_measurements(values, run.metrics, expected_count=run.count)
        run.logger.info(f"Statistics for {run.test_id}: " + ", ".join(
            f"{name}={value:.6g}" if value is not None else f"{name}=n/a"
            for name, value in analysis.items()
        ))
        control = control_chart_analysis(values, run.chart_rules.get("sigma_limit"), run.chart_rules.get("run_length"))
        if control is not None and not control["in_control"]:
            trend = control["trend"]
            run.warn(
                f"{run.test_id} is out of statistical control: {len(control['beyond_limits'])} reading(s) beyond "
                f"±{control['sigma_limit']:g} sigma, {len(control['runs'])} run(s) of "
                f"{control['run_length']}+ on one side of the median"
                + (f", significant trend {trend['relative_slope_per_reading']:+.2%} per reading "
                   f"(p={trend['p_value']:.3g})" if trend and trend["significant"] else "")
            )

        return {
            "test_id": run.test_id,
            "ammeter_type": run.ammeter_type,
            "port": run.ammeter["port"],
            "command": run.ammeter["command"],
            "sampling": {
                "measurements_count": run.count,
                "total_duration_seconds": run.duration,
                "sampling_frequency_hz": run.frequency,
            },
            "start_time": sampled["start_time"].isoformat(),
            "actual_duration_seconds": sampled["actual_duration"],
            "timing": sampled["timing"],
            "measurements": measurements,
            "values": values,
            "errors": errors,
            "error_counts": dict(Counter(e["type"] for e in errors)),
            "statistics": analysis,
            "control_chart": control,
            "aliasing": aliasing,
            "warnings": run.warnings,
            "log_file": run.logger.log_file,
            "plots": [],
            "plot_error": None,
            "archive_path": None,
            "archive_error": None,
        }

    def _persist(self, run: _Run, result: Dict) -> None:
        """Charts and archive record. A failure of either is logged and recorded; the readings are never lost."""
        if self.plots_enabled:
            if not result["values"]:
                run.warn(f"No readings for {run.test_id}; skipping plots")
            else:
                try:
                    result["plots"] = create_run_plots(result, self.plots_dir, self.plot_types,
                                                       self._ammeter_order())
                    run.logger.info(f"Saved plots for {run.test_id}: {result['plots']}")
                except Exception as e:
                    # Plots are a by-product; a drawing failure must not cost the run its data
                    result["plot_error"] = f"{type(e).__name__}: {e}"
                    run.logger.error(f"Failed to plot {run.test_id}: {result['plot_error']}")

        if self.archive_enabled:
            # Set before saving so the archived record knows its own location
            result["archive_path"] = self.archive.path_for(run.test_id)
            try:
                self.archive.save(result, self.config, self.config_path)
                run.logger.info(f"Archived {run.test_id} to {result['archive_path']}")
            except (OSError, TypeError, ValueError) as e:
                result["archive_path"] = None
                result["archive_error"] = f"{type(e).__name__}: {e}"
                run.logger.error(f"Failed to archive {run.test_id}: {result['archive_error']}")

    def new_test_id(self, ammeter_type: str) -> str:
        """A test ID not yet in the archive: `<ammeter>_<8 hex>`, re-drawn in the unlikely case it's taken."""
        while True:
            test_id = f"{ammeter_type}_{uuid.uuid4().hex[:8]}"
            if not self.archive.exists(test_id):
                return test_id

    def ammeter_config(self, ammeter_type: str) -> Dict:
        """The validated config entry for one ammeter; ValueError if it is missing or invalid."""
        ammeters = self.config.get("ammeters") or {}
        if ammeter_type not in ammeters:
            raise ValueError(f"Unknown ammeter type '{ammeter_type}'. Configured: {sorted(ammeters)}")
        ammeter = ammeters[ammeter_type] or {}
        validate_ammeter(ammeter_type, ammeter)
        return ammeter
