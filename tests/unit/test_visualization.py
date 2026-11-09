import os
import random

import allure
import pytest

from src.analysis import visualization as viz
from src.analysis.result_analysis import control_chart_analysis

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ORDER = ["greenlee", "entes", "circutor"]


def _subdir(tmp_path, n):
    """A separate folder per merged part, so parts never see each other's files."""
    path = tmp_path / f"part{n}"
    path.mkdir()
    return path


def check_png(path) -> None:
    """Assert the file is a PNG and attach it, so the report shows the chart itself."""
    with open(path, "rb") as f:
        assert f.read(8) == PNG_SIGNATURE, path
    allure.attach.file(path, name=os.path.basename(path), attachment_type=allure.attachment_type.PNG)


def record(values, ammeter_type="entes", test_id="entes_plot0000", errors=()):
    rng = random.Random(0)
    measurements = [{"index": i, "elapsed_s": i / 10 + rng.uniform(0, 0.002), "value": v} for i, v in enumerate(values)]
    return {
        "test_id": test_id,
        "ammeter_type": ammeter_type,
        "sampling": {"measurements_count": len(values) + len(errors), "total_duration_seconds": len(values) / 10,
                     "sampling_frequency_hz": 10.0},
        "measurements": measurements,
        "values": list(values),
        "errors": list(errors),
        "statistics": {"coefficient_of_variation": 0.5},
        "control_chart": control_chart_analysis(values),
    }


def noisy(n=30, seed=0):
    rng = random.Random(seed)
    return [rng.gauss(50, 10) for _ in range(n)]


def _part_every_chart_renders_every_case(tmp_path):
    """Each chart type renders for normal, single and identical readings, and with failures and flagged rules."""
    cases = {"normal": noisy(), "single-reading": [42.0], "identical": [5.0] * 20}
    for name, values in cases.items():
        for plot_type in viz.PLOT_TYPES:
            with allure.step(f"{plot_type} renders for {name} ({len(values)} readings)"):
                paths = viz.create_run_plots(record(values, test_id=f"entes_{name}"), str(tmp_path), [plot_type],
                                             ORDER)
                assert len(paths) == 1
                check_png(paths[0])
    with allure.step("Drift + a ×8 spike + a timed-out reading: out of control, and every chart still renders"):
        values = [v * (1 + 0.03 * i) for i, v in enumerate(noisy(40))]
        values[35] *= 8
        flagged = record(values, test_id="entes_flagged",
                         errors=[{"index": 40, "elapsed_s": 4.0, "type": "TimeoutError", "error": "timed out"}])
        assert not flagged["control_chart"]["in_control"]
        for path in viz.create_run_plots(flagged, str(tmp_path), list(viz.PLOT_TYPES), ORDER):
            check_png(path)
    with allure.step("No readings: no charts at all"):
        assert viz.create_run_plots(record([]), str(tmp_path), list(viz.PLOT_TYPES), ORDER) == []
    with allure.step("Positive readings whose LCL computes below 0 A (heavy-tailed, like Greenlee): the limit is "
                     "drawn at 0 A and labelled with the computed value"):
        lcl_record = record([0.05, 0.03, 0.4, 0.02, 0.06, 0.04, 0.03, 0.05, 0.07, 0.02], "greenlee", "greenlee_lcl")
        assert lcl_record["control_chart"]["lcl"] < 0
        figures = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(viz, "_save", lambda fig, path: figures.append(fig) or path)
            viz.plot_control_chart(lcl_record, str(tmp_path / "lcl.png"), ORDER)
        labels = [text.get_text() for text in figures[0].axes[0].texts]
        assert any(label.startswith("LCL 0 A\n(computed -") for label in labels), labels


def _part_comparison_colors_and_chart_selection(tmp_path):
    """The comparison skips runs it can't normalise; colors follow the ammeter; chart types are validated."""
    empty, zero_mean = record([]), record([-1.0, 1.0])
    with allure.step("Only empty or zero-mean runs: nothing to draw (None)"):
        assert viz.plot_comparison([empty, zero_mean], str(tmp_path / "c.png"), ORDER) is None
    with allure.step("Three normal runs plus an empty one: drawn without the empty run"):
        good = [record(noisy(seed=s), t, f"{t}_{s:08d}") for s, t in enumerate(ORDER)]
        check_png(viz.plot_comparison(good + [empty], str(tmp_path / "c.png"), ORDER))
    with allure.step("Each ammeter keeps its own color; three ammeters, three colors; unknown -> gray"):
        assert viz.series_color("entes", ORDER) == viz.series_color("entes", list(ORDER))
        assert len({viz.series_color(t, ORDER) for t in ORDER}) == 3
        assert viz.series_color("unknown", ORDER) == viz.TEXT_MUTED
    with allure.step("None or [] selects every chart type; 'pie' is rejected"):
        assert viz.validate_plot_types(None) == viz.validate_plot_types([]) == list(viz.PLOT_TYPES)
        with pytest.raises(ValueError, match="pie"):
            viz.validate_plot_types(["pie"])


def test_the_charts(tmp_path):
    """Every chart renders every case; colours follow the ammeter; chart selection is validated."""
    with allure.step(
            "Each chart type renders for normal, single and identical readings, and with failures and flagged "
            "rules."):
        _part_every_chart_renders_every_case(_subdir(tmp_path, 1))
    with allure.step(
            "The comparison skips runs it can't normalise; colors follow the ammeter; chart types are validated."):
        _part_comparison_colors_and_chart_selection(_subdir(tmp_path, 2))
