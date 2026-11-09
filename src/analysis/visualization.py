import os
import random
import statistics
from typing import Any, Callable, Dict, List, Optional, Sequence

import matplotlib
from matplotlib.figure import Figure

from src.analysis.result_analysis import control_chart_analysis

# Charts are written to PNG files, so only the light theme is needed.
# Categorical slots 1-3 validated all-pairs against SURFACE (CVD and normal-vision
# floors pass). Slot 3 (aqua) is below 3:1 contrast, so every chart names its
# ammeter in text (title, tick labels or legend) rather than relying on color.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
AXIS = "#c3c2b7"
STATUS_CRITICAL = "#d03b3b"
STATUS_WARNING = "#fab219"

_RC: Any = {     # matplotlib rcParams overrides; Any because its stubs type the keys as literals
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"],
    "font.size": 9,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "axes.labelcolor": TEXT_SECONDARY,
    "xtick.color": TEXT_MUTED,
    "ytick.color": TEXT_MUTED,
    "xtick.labelcolor": TEXT_SECONDARY,
    "ytick.labelcolor": TEXT_SECONDARY,
}


def series_color(ammeter_type: str, ammeter_order: Sequence[str]) -> str:
    """Color follows the ammeter (its position in the config), never its position in a chart."""
    if ammeter_type in ammeter_order:
        index = list(ammeter_order).index(ammeter_type)
        if index < len(SERIES_COLORS):
            return SERIES_COLORS[index]
    return TEXT_MUTED


def _new_figure(width: float = 7.5, height: float = 4.0):
    fig = Figure(figsize=(width, height), dpi=150, facecolor=SURFACE)
    ax = fig.add_subplot(facecolor=SURFACE)
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    return fig, ax


def _titles(fig, title: str, subtitle: str):
    fig.text(0.02, 0.965, title, ha="left", va="top", fontsize=12, fontweight="semibold", color=TEXT_PRIMARY)
    fig.text(0.02, 0.90, subtitle, ha="left", va="top", fontsize=8.5, color=TEXT_SECONDARY)


def _readings(count: int) -> str:
    return f"{count} reading{'s' if count != 1 else ''}"


def _save(fig, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    return path


def plot_time_series(record: Dict, path: str, ammeter_order: Sequence[str]) -> str:
    """Current against elapsed time, with the run mean, a ±1 SD band, and failed requests marked."""
    measurements = record["measurements"]
    stats = record.get("statistics", {})
    color = series_color(record["ammeter_type"], ammeter_order)
    elapsed = [m["elapsed_s"] for m in measurements]
    values = [m["value"] for m in measurements]

    with matplotlib.rc_context(_RC):
        fig, ax = _new_figure()
        mean = statistics.fmean(values)
        std_dev = statistics.stdev(values) if len(values) >= 2 else None
        if std_dev is not None:
            ax.axhspan(mean - std_dev, mean + std_dev, color=color, alpha=0.10, linewidth=0)
        ax.axhline(mean, color=TEXT_SECONDARY, linewidth=1)
        ax.plot(elapsed, values, color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round",
                marker="o", markersize=6, markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=1.5,
                zorder=3, clip_on=False)
        ax.annotate(f"mean {mean:.4g} A", xy=(1, mean), xycoords=("axes fraction", "data"),
                    xytext=(4, 0), textcoords="offset points", va="center", ha="left",
                    fontsize=8.5, color=TEXT_SECONDARY)

        errors = record.get("errors", [])
        if errors:
            ax.plot([e["elapsed_s"] for e in errors], [0] * len(errors), linestyle="none", marker="x",
                    markersize=7, markeredgewidth=2, color=STATUS_CRITICAL, zorder=3,
                    transform=ax.get_xaxis_transform(), clip_on=False)
            ax.annotate(f"× {len(errors)} failed request{'s' if len(errors) != 1 else ''}",
                        xy=(0, 0), xycoords="axes fraction", xytext=(0, -30), textcoords="offset points",
                        fontsize=8.5, color=TEXT_SECONDARY)

        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("Current (A)")
        # Always show the whole sampling window, so gaps and early/late readings are visible
        window = max(record["sampling"]["total_duration_seconds"], max(elapsed))
        ax.set_xlim(0, window)
        band = "shaded band = mean ± 1 SD" if std_dev is not None else "single reading, no SD"
        _titles(fig, f"{record['ammeter_type'].upper()} current over time",
                f"{record['test_id']} · {_readings(len(values))} at "
                f"{record['sampling']['sampling_frequency_hz']:g} Hz · {band}"
                + (f" · CV {stats['coefficient_of_variation']:.3g}"
                   if stats.get("coefficient_of_variation") is not None else ""))
        fig.subplots_adjust(left=0.09, right=0.85, top=0.80, bottom=0.17)
        return _save(fig, path)


def plot_histogram(record: Dict, path: str, ammeter_order: Sequence[str]) -> str:
    """Distribution of the run's readings, with mean and median marked."""
    values = record["values"]
    color = series_color(record["ammeter_type"], ammeter_order)

    with matplotlib.rc_context(_RC):
        fig, ax = _new_figure()
        # Surface-colored edges give the 2px gap between adjacent bars
        counts, _, _ = ax.hist(values, bins="auto", color=color, edgecolor=SURFACE, linewidth=1.5, zorder=2)
        ax.yaxis.get_major_locator().set_params(integer=True)
        # Headroom so the mean/median labels sit above the tallest bar
        ax.set_ylim(0, max(counts) * 1.18)

        mean = statistics.fmean(values)
        median = statistics.median(values)
        # Put each label on the outer side of its line so they don't collide when mean ≈ median
        mean_side = "left" if mean >= median else "right"
        for value, name, ink, side in ((mean, "mean", TEXT_PRIMARY, mean_side),
                                       (median, "median", TEXT_MUTED, "right" if mean_side == "left" else "left")):
            ax.axvline(value, color=ink, linewidth=1, zorder=3)
            ax.annotate(f"{name} {value:.4g} A", xy=(value, 1), xycoords=("data", "axes fraction"),
                        xytext=(4 if side == "left" else -4, -2), textcoords="offset points",
                        ha=side, va="top", fontsize=8.5, color=ink)

        ax.set_xlabel("Current (A)")
        ax.set_ylabel("Readings")
        _titles(fig, f"{record['ammeter_type'].upper()} reading distribution",
                f"{record['test_id']} · {_readings(len(values))}")
        fig.subplots_adjust(left=0.09, right=0.97, top=0.80, bottom=0.15)
        return _save(fig, path)


def plot_control_chart(record: Dict, path: str, ammeter_order: Sequence[str]) -> str:
    """
    Individuals control chart: readings in order against the mean and ±3 sigma
    limits, with readings beyond the limits, runs on one side of the median, and a
    significant trend flagged (see result_analysis.control_chart_analysis).
    Status markers carry a shape and a legend label, never color alone.
    """
    values = record["values"]
    control = record.get("control_chart") or control_chart_analysis(values)
    color = series_color(record["ammeter_type"], ammeter_order)
    positions = list(range(1, len(values) + 1))

    with matplotlib.rc_context(_RC):
        fig, ax = _new_figure(height=4.4)
        if control is None:
            ax.plot(positions, values, color=color, linewidth=2, marker="o", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, clip_on=False)
            subtitle = "a control chart needs at least 2 readings"
        else:
            for run in control["runs"]:
                ax.axvspan(run["start"] + 0.5, run["end"] + 1.5, color=STATUS_WARNING, alpha=0.12, linewidth=0)
            # Limits are around the mean; the run rule uses the median (drawn only when runs are flagged)
            reference_lines = [(control["ucl"], "UCL", TEXT_MUTED, ""),
                               (control["center"], "mean", TEXT_SECONDARY, "")]
            if control["lcl"] < 0 <= min(values):
                # A current that is never negative can't fall below 0 A: draw the limit there and say so,
                # instead of a line (and a stretched axis) below anything that can be measured
                reference_lines.append((0.0, "LCL", TEXT_MUTED, f"\n(computed {control['lcl']:.3g})"))
            else:
                reference_lines.append((control["lcl"], "LCL", TEXT_MUTED, ""))
            if control["runs"]:
                reference_lines.append((control["median"], "median", TEXT_MUTED, ""))
            for value, label, ink, note in reference_lines:
                ax.axhline(value, color=ink, linewidth=1, zorder=1)
                ax.annotate(f"{label} {value:.4g} A{note}", xy=(1, value), xycoords=("axes fraction", "data"),
                            xytext=(4, 0), textcoords="offset points", va="center", ha="left",
                            fontsize=8.5, color=TEXT_SECONDARY)
            ax.plot(positions, values, color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round",
                    marker="o", markersize=6, markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=1.5,
                    zorder=3, clip_on=False)

            trend = control.get("trend")
            if trend and trend["significant"]:
                # Sen's line, fitted on 0-based reading indexes
                ends = [0, len(values) - 1]
                relative = trend["relative_slope_per_reading"]
                line = [trend["sen_intercept"] + trend["sen_slope_per_reading"] * i for i in ends]
                ax.plot([i + 1 for i in ends], line,
                        color=TEXT_PRIMARY, linewidth=1.5, zorder=2,
                        label=(f"Trend {relative:+.2%} per reading" if relative is not None else "Trend")
                              + f" (p={trend['p_value']:.2g})")

            in_run = sorted({i for run in control["runs"] for i in range(run["start"], run["end"] + 1)}
                            - set(control["beyond_limits"]))
            if in_run:
                ax.plot([i + 1 for i in in_run], [values[i] for i in in_run], linestyle="none", marker="D",
                        markersize=6, markerfacecolor=STATUS_WARNING, markeredgecolor=SURFACE, markeredgewidth=1.5,
                        zorder=4, clip_on=False, label=f"Run of {control['run_length']}+ on one side of the median")
            if control["beyond_limits"]:
                ax.plot([i + 1 for i in control["beyond_limits"]], [values[i] for i in control["beyond_limits"]],
                        linestyle="none", marker="X", markersize=9, markerfacecolor=STATUS_CRITICAL,
                        markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=5, clip_on=False,
                        label=f"Beyond ±{control['sigma_limit']:g}σ limits")
            if ax.get_legend_handles_labels()[0]:
                ax.legend(loc="upper left", bbox_to_anchor=(0, -0.16), ncol=3, frameon=False, fontsize=8.5,
                          labelcolor=TEXT_SECONDARY, handletextpad=0.3, columnspacing=1.5)

            if control["in_control"]:
                subtitle = "In control: no reading beyond the limits, no long runs, no significant trend"
            else:
                problems = []
                if control["beyond_limits"]:
                    problems.append(f"{len(control['beyond_limits'])} beyond ±{control['sigma_limit']:g}σ")
                if control["runs"]:
                    problems.append(f"{len(control['runs'])} run(s) of {control['run_length']}+ on one side")
                if trend and trend["significant"]:
                    problems.append("significant trend")
                subtitle = "Out of control: " + " · ".join(problems)

        ax.set_xlabel("Reading (in order)")
        ax.set_ylabel("Current (A)")
        ax.set_xlim(0.5, len(values) + 0.5)
        ax.xaxis.get_major_locator().set_params(integer=True)
        _titles(fig, f"{record['ammeter_type'].upper()} control chart",
                f"{record['test_id']} · {_readings(len(values))} · {subtitle}")
        fig.subplots_adjust(left=0.09, right=0.84, top=0.80, bottom=0.25)
        return _save(fig, path)


def plot_comparison(records: Sequence[Dict], path: str, ammeter_order: Sequence[str]) -> Optional[str]:
    """
    Spread of each run's readings on one shared scale.

    The ammeters' readings differ by orders of magnitude, so instead of two y-scales
    every reading is divided by its own run's mean (1.0 = the run mean). The spread
    around 1.0 is the run's consistency; each run's coefficient of variation is
    labelled above it. Runs with no readings or a zero mean are left out.
    Returns None if no run can be plotted.
    """
    runs = []
    for record in records:
        values = record.get("values") or []
        mean = statistics.fmean(values) if values else 0
        if mean:
            runs.append((record, [v / mean for v in values]))
    if not runs:
        return None

    jitter = random.Random(0)
    with matplotlib.rc_context(_RC):
        fig, ax = _new_figure(width=max(6.0, 1.6 * len(runs) + 2.0), height=4.5)
        ax.axhline(1.0, color=TEXT_SECONDARY, linewidth=1, zorder=1)

        seen_types = []
        for position, (record, normalized) in enumerate(runs):
            ammeter_type = record["ammeter_type"]
            color = series_color(ammeter_type, ammeter_order)
            ax.boxplot([normalized], positions=[position], widths=0.35, showfliers=False,
                       patch_artist=True, zorder=2,
                       boxprops={"facecolor": matplotlib.colors.to_rgba(color, 0.10), "edgecolor": color,
                                 "linewidth": 1},
                       medianprops={"color": color, "linewidth": 2},
                       whiskerprops={"color": color, "linewidth": 1},
                       capprops={"color": color, "linewidth": 1})
            ax.scatter([position + jitter.uniform(-0.12, 0.12) for _ in normalized], normalized,
                       s=30, color=color, edgecolors=SURFACE, linewidths=1.5, zorder=3,
                       label=ammeter_type.upper() if ammeter_type not in seen_types else None)
            if ammeter_type not in seen_types:
                seen_types.append(ammeter_type)

            cv = (record.get("statistics") or {}).get("coefficient_of_variation")
            if cv is not None:
                ax.annotate(f"CV {cv:.3g}", xy=(position, 1), xycoords=("data", "axes fraction"),
                            xytext=(0, 4), textcoords="offset points", ha="center", va="bottom",
                            fontsize=8.5, color=TEXT_SECONDARY, annotation_clip=False)

        ax.set_xticks(range(len(runs)))
        ax.set_xticklabels([f"{r['ammeter_type'].upper()}\n{r['test_id'].rsplit('_', 1)[-1]}" for r, _ in runs])
        ax.set_xlim(-0.6, len(runs) - 0.4)
        ax.set_ylabel("Reading ÷ run mean")
        if len(seen_types) >= 2:
            ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8.5,
                      labelcolor=TEXT_SECONDARY, handletextpad=0.3)
        _titles(fig, "Measurement spread by run",
                "Each reading divided by its run's mean (line at 1.0) · box = median and quartiles "
                "· CV = std dev ÷ mean")
        fig.subplots_adjust(left=0.08, right=0.84 if len(seen_types) >= 2 else 0.97, top=0.76, bottom=0.16)
        return _save(fig, path)


# Plots made for each run; each takes (record, path, ammeter_order)
PLOT_TYPES: Dict[str, Callable[[Dict, str, Sequence[str]], str]] = {
    "time_series": plot_time_series,
    "histogram": plot_histogram,
    "control_chart": plot_control_chart,
}


def validate_plot_types(plot_types: Optional[List[str]]) -> List[str]:
    """Return the per-run plot types to draw; None or empty means all of them."""
    if not plot_types:
        return list(PLOT_TYPES)
    unknown = [p for p in plot_types if p not in PLOT_TYPES]
    if unknown:
        raise ValueError(f"Unknown plot types {unknown}. Available: {list(PLOT_TYPES)}")
    return list(plot_types)


def create_run_plots(record: Dict, output_dir: str, plot_types: Sequence[str],
                     ammeter_order: Sequence[str]) -> List[str]:
    """Draw the requested plots for one run as `<output_dir>/<test_id>_<plot_type>.png`."""
    if not record.get("values"):
        return []
    return [PLOT_TYPES[plot_type](record, os.path.join(output_dir, f"{record['test_id']}_{plot_type}.png"),
                                  ammeter_order)
            for plot_type in plot_types]
