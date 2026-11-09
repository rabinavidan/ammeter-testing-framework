"""
Check every requirement of Exam/ammeter-test-specification.pdf against the running code, with evidence.

    python tools/spec_audit.py

Each line is a requirement from the specification, PASS/FAIL/SKIP, and what was measured. Checks test behaviour
against independently known answers (hand-computed statistics, a known reference current, a configuration change
that must change the run), not the presence of files. Everything runs live in a temporary folder on free ports
(nothing is written to results/). SKIP means "could not be verified here" and is never counted as a pass.
Exit code 1 if any requirement fails.
"""
import ast
import contextlib
import glob
import importlib.metadata
import io
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Ammeters.base_ammeter import AmmeterEmulatorBase  # noqa: E402
from Ammeters.Circutor_Ammeter import CircutorAmmeter  # noqa: E402
from Ammeters.Entes_Ammeter import EntesAmmeter  # noqa: E402
from Ammeters.error_simulation import TRANSPORT_FAULTS, ErrorSimulator  # noqa: E402
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter  # noqa: E402
from src.analysis.accuracy import assess_accuracy, format_accuracy_report  # noqa: E402
from src.analysis.result_analysis import analyze_measurements, control_chart_analysis  # noqa: E402
from src.testing.test_framework import AmmeterTestFramework, resolve_sampling  # noqa: E402

CONFIG = os.path.join(ROOT, "config", "config.yaml")
CLASSES: Dict[str, Callable[..., AmmeterEmulatorBase]] = {
    "greenlee": GreenleeAmmeter, "entes": EntesAmmeter, "circutor": CircutorAmmeter}
results: List[str] = []


def check(section: str, requirement: str, passed: Optional[bool], evidence: str) -> None:
    """passed=None records SKIP: the requirement could not be verified in this environment."""
    status = "SKIP" if passed is None else "PASS" if passed else "FAIL"
    results.append(status)
    print(f"[{status}] {section:<22} {requirement}\n         {evidence}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def start(emulators):
    threads = [threading.Thread(target=e.start_server, daemon=True) for e in emulators]
    for t in threads:
        t.start()
    for e in emulators:
        e.ready.wait(5)
    return threads


def stop(emulators, threads):
    for e in emulators:
        e.stop()
    for t in threads:
        t.join(timeout=10)


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def main() -> int:
    work = tempfile.mkdtemp(prefix="spec-audit-")
    os.chdir(work)

    # --- Before you start: make main.py work and return data from the ammeters -----------------------
    ports_free = all(socket.socket().connect_ex(("localhost", p)) != 0 for p in (5000, 5001, 5002))
    if ports_free:
        out = subprocess.run([sys.executable, "main.py"], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout
        received = [line for line in out.splitlines() if line.startswith("Received current measurement")]
        check("Before you start", "main.py works and returns data from the ammeters", len(received) == 3,
              f"{len(received)}/3 readings: " + "; ".join(r.split(": ", 1)[1] for r in received))
    else:
        check("Before you start", "main.py works and returns data from the ammeters", None,
              "not verified: ports 5000-5002 are in use by another program")

    ports = {name: free_port() for name in CLASSES}
    framework = AmmeterTestFramework(CONFIG, overrides={"ammeters": {n: {"port": p} for n, p in ports.items()}})
    emulators = [cls(ports[name]) for name, cls in CLASSES.items()]
    threads = start(emulators)
    try:
        runs = {name: quiet(framework.run_test, name) for name in CLASSES}
        second = quiet(framework.run_test, "entes")
        # A second framework that differs only in its config: the run must follow it
        configured = AmmeterTestFramework(CONFIG, overrides={
            "ammeters": {n: {"port": p} for n, p in ports.items()},
            "testing": {"sampling": {"measurements_count": 7, "sampling_frequency_hz": 25,
                                     "total_duration_seconds": None}},
            "analysis": {"statistical_metrics": ["mean", "max"],
                         "visualization": {"plot_types": ["histogram"], "output_dir": "cfg/plots"}},
            "result_management": {"enabled": False}})
        reconfigured = quiet(configured.run_test, "circutor")
        # A run where every reading fails (nothing listens on the port) must report in the same shape
        failing_framework = AmmeterTestFramework(CONFIG, overrides={
            "testing": {"request_timeout_seconds": 0.3},
            "analysis": {"visualization": {"enabled": False}}, "result_management": {"enabled": False}})
        failing_framework.register_ammeter("dead", free_port(), "MEASURE_ENTES -get_data")
        failing = quiet(failing_framework.run_test, "dead", {"measurements_count": 3, "sampling_frequency_hz": 10,
                                                             "total_duration_seconds": None})
    finally:
        stop(emulators, threads)

    # --- 1. Unified measurement API ----------------------------------------------------------------------
    key_sets = {name: frozenset(r) for name, r in runs.items()}
    check("1. Unified API", "one interface works with all three ammeter types",
          all(len(r["values"]) == r["sampling"]["measurements_count"] for r in runs.values()),
          "run_test(type) -> " + ", ".join(f"{n}: {len(r['values'])} readings" for n, r in runs.items()))
    stat_keys = {frozenset(x["statistics"]) for x in list(runs.values()) + [failing]}
    failed_cleanly = (not failing["values"] and len(failing["errors"]) == 3
                      and failing["statistics"]["success_rate"] == 0 and failing["statistics"]["mean"] is None)
    check("1. Unified API", "consistent result reporting",
          len(set(key_sets.values()) | {frozenset(failing)}) == 1 and len(stat_keys) == 1 and failed_cleanly,
          f"identical result structure for all types and for a run where every reading failed "
          f"({len(next(iter(key_sets.values())))} fields, {len(next(iter(stat_keys)))} statistics; the failed run: "
          f"{failing['error_counts']}, success rate 0, metrics None)")

    # --- 2. Measurement sampling -------------------------------------------------------------------------
    combos = [resolve_sampling(c) for c in ({"measurements_count": 10, "sampling_frequency_hz": 5},
                                             {"measurements_count": 10, "total_duration_seconds": 2},
                                             {"total_duration_seconds": 2, "sampling_frequency_hz": 5})]
    check("2. Sampling", "configurable count, duration and frequency (any two set the third)",
          combos == [(10, 2.0, 5.0)] * 3, f"three configurations all resolve to {combos[0]}")
    r = runs["greenlee"]
    f = r["sampling"]["sampling_frequency_hz"]
    drift_ms = max(abs(m["elapsed_s"] - m["index"] / f) for m in r["measurements"]) * 1000
    server_ms, server_hz, server_run = server_side_timing(framework)
    check("2. Sampling", "precise timing and data collection",
          drift_ms < 50 and not r["errors"] and server_ms is not None and server_ms < 1000 / server_hz / 2
          and not server_run["errors"],
          f"worst offset from schedule {drift_ms:.1f} ms (framework clock, {f:g} Hz); run lasted "
          f"{r['actual_duration_seconds']:.3f} s of {r['sampling']['total_duration_seconds']:g} s; 0 failed readings. "
          f"Timed by a device in another process at {server_hz:g} Hz: arrivals at worst "
          + ("n/a" if server_ms is None else f"{server_ms:.1f} ms")
          + f" from schedule (limit: half the {1000 / server_hz:g} ms interval)")

    # --- 3. Result analysis ------------------------------------------------------------------------------
    s = r["statistics"]
    # Known data with hand-computed answers: mean 40/8 = 5, median (4+5)/2 = 4.5, sample std dev sqrt(32/7)
    known = analyze_measurements([2, 4, 4, 4, 5, 5, 7, 9])
    by_hand = {"mean": 5.0, "median": 4.5, "std_dev": math.sqrt(32 / 7), "min": 2, "max": 9}
    check("3. Result analysis", "mean, median, standard deviation, min and max",
          all(abs(known[k] - by_hand[k]) < 1e-12 for k in by_hand) and all(s[k] is not None for k in by_hand),
          "[2,4,4,4,5,5,7,9] -> mean 5, median 4.5, std dev 2.13809 (n-1), min 2, max 9, as computed by hand; "
          "all five reported for every live run")
    plots = [os.path.basename(p) for p in r["plots"]]
    check("3. Bonus", "visualization of measurement data", len(plots) == 3 and all(os.path.isfile(p) for p in
                                                                                  r["plots"]),
          f"{len(plots)} charts per run: " + ", ".join(p.split('_', 2)[-1] for p in plots))
    # Behaviour, not presence: stable data must stay in control at any length, and a slow drift must be flagged.
    # Stable readings follow the CIRCUTOR formula (10 voltages x one random time step), 2,000 of them.
    rng = random.Random(7)
    stable = [sum(rng.uniform(0.1, 1.0) for _ in range(10)) * rng.uniform(0.001, 0.01) for _ in range(2000)]
    stable_chart = control_chart_analysis(stable)
    drifting_chart = control_chart_analysis([v * (1 + 0.0005 * i) for i, v in enumerate(stable)])
    assert stable_chart is not None and drifting_chart is not None      # 2,000 readings: always a chart
    consistency = (r["control_chart"] is not None and "coefficient_of_variation" in s
                   and stable_chart["in_control"] and not drifting_chart["in_control"])
    check("3. Bonus", "performance consistency evaluation", consistency,
          f"CV and control chart per run; 2,000 stable CIRCUTOR-formula readings: in control "
          f"({stable_chart['sigma_limit']:.2f} sigma, runs of {stable_chart['run_length']}); the same with a "
          f"0.05 %/reading drift: flagged")

    # --- 4. Result management ----------------------------------------------------------------------------
    ids = [x["test_id"] for x in list(runs.values()) + [second]]
    record = framework.archive.load(runs["entes"]["test_id"])
    listed = framework.archive.list_runs("entes")
    compared = framework.archive.compare([runs["entes"]["test_id"], second["test_id"]], ["mean"])
    check("4. Result management", "unique identification for each test run", len(set(ids)) == len(ids),
          f"{len(ids)} runs, {len(set(ids))} distinct IDs, e.g. {ids[0]}")
    check("4. Result management", "metadata storage", set(record["metadata"]) >= {"archived_at", "platform", "config"},
          "archived with " + ", ".join(sorted(record["metadata"])))
    check("4. Result management", "easy retrieval and comparison of historical results",
          len(listed) == 2 and len(compared) == 2,
          f"list_runs('entes') -> {len(listed)} runs; compare -> means " +
          ", ".join(f"{row['mean']:.4g}" for row in compared.values()))

    # --- 5. Accuracy assessment --------------------------------------------------------------------------
    assessment = quiet(framework.assess_accuracy, list(runs.values()) + [second], save=False)
    per_type = assessment["per_type"]
    check("5. Accuracy (bonus)", "compare measurements across ammeter types", len(assessment["agreement"]) == 3,
          "pairwise agreement: " + ", ".join(f"{p['a']}/{p['b']} {p['median_ratio']:.3g}x" for p in
                                              assessment["agreement"]))
    # Known truth: a reads 10 A precisely, b reads 10 A noisily, c reads 20 A precisely. Against a 10 A reference
    # the most accurate is a, then b, then c; without a reference c looks best (precision) and must say so
    rng = random.Random(0)
    truth = [{"test_id": f"{t}_0", "ammeter_type": t, "values": [rng.gauss(mu, sd) for _ in range(200)],
              "sampling": {"measurements_count": 200}} for t, mu, sd in (("a", 10, 0.1), ("b", 10, 1), ("c", 20, 0.1))]
    with_ref = assess_accuracy(truth, reference_current_a=10, resamples=300)
    without = assess_accuracy(truth, resamples=300)
    live_text = format_accuracy_report(assessment)
    honest = ("not established" in live_text) != assessment["clear_winner"]
    check("5. Accuracy (bonus)", "determine relative accuracy and the most reliable method",
          with_ref["ranking"] == ["a", "b", "c"] and without["ranking"][0] == "c"
          and any("not accuracy" in n for n in without["notes"]) and honest,
          f"known data vs a 10 A reference ranks a > b > c (the true order); without a reference it ranks spread "
          f"and says so; live: {' > '.join(assessment['ranking'])}, clear winner {assessment['clear_winner']}, and "
          f"the report {'names the winner' if assessment['clear_winner'] else 'declines to name one'} accordingly")
    check("5. Accuracy (bonus)", "statistical techniques to quantify measurement precision",
          all(t["mean_ci"] and t["expanded_uncertainty_k2_a"] for t in per_type.values()),
          "bootstrap 95 % CIs, robust CV, GUM Type A uncertainty (k=2) for every type")

    # --- Bonus challenges --------------------------------------------------------------------------------
    simulator = ErrorSimulator({"dropped_connection": {"probability": 0.2}, "corrupted_response": {"probability": 0.1},
                                "truncated_response": {"probability": 0.1}}, seed=1)
    port = free_port()
    faulty = [FixedPortAmmeter(port, simulator)]
    threads = start(faulty)
    try:
        framework.register_ammeter("faulty", port, "MEASURE_ENTES -get_data")
        run = quiet(framework.run_test, "faulty", {"measurements_count": 40, "sampling_frequency_hz": 40,
                                                   "total_duration_seconds": None})
    finally:
        stop(faulty, threads)
    injected = sum(simulator.counts()[name] for name in TRANSPORT_FAULTS)
    check("Bonus", "error simulation", injected > 0 and len(run["errors"]) == injected,
          f"{injected} faults injected, {len(run['errors'])} detected and recorded; {len(run['values'])} good readings")
    comparison = quiet(framework.plot_comparison, list(runs.values()))
    control_png = [p for p in r["plots"] if p.endswith("_control_chart.png")]
    limits = r["control_chart"]
    check("Bonus", "advanced visualization techniques",
          bool(control_png) and os.path.getsize(control_png[0]) > 10_000 and limits["ucl"] is not None
          and comparison is not None and os.path.getsize(comparison) > 10_000,
          f"control chart drawn with limits {limits['lcl']:.3g}..{limits['ucl']:.3g} A plus run and "
          f"trend rules; normalized comparison of {len(runs)} ammeters ({os.path.basename(comparison or '-')})")
    rejected = 0
    invalid = ({"testing": {"request_timeout_seconds": "abc"}}, {"ammeters": {"entes": {"port": 70000}}},
               {"testing": {"sampling": {"sampling_frequency_hz": -5}}},
               {"analysis": {"statistical_metrics": ["mean", "modee"]}},
               {"analysis": {"accuracy": {"confidence_level": 5}}}, {"result_management": {"enabled": "false"}},
               {"error_simulation": {"enabled": True, "faults": {"gremlins": {}}}})
    for bad in invalid:
        try:
            AmmeterTestFramework(CONFIG, overrides=bad)     # construction only: no request may be needed to see it
        except ValueError:
            rejected += 1
    follows = (len(reconfigured["measurements"]) + len(reconfigured["errors"]) == 7
               and reconfigured["sampling"]["sampling_frequency_hz"] == 25
               and set(reconfigured["statistics"]) >= {"mean", "max"} and "median" not in reconfigured["statistics"]
               and [os.path.basename(p).rsplit("_", 1)[-1] for p in reconfigured["plots"]] == ["histogram.png"]
               and reconfigured["archive_path"] is None)
    check("Bonus", "configuration-driven testing approach", follows and rejected == len(invalid),
          "changing only the config changed the run (7 readings at 25 Hz, metrics mean+max, histogram only, "
          f"no archive); {rejected}/{len(invalid)} invalid settings (sampling, port, timeout, metric, accuracy, "
          f"switch, fault) rejected with ValueError when the framework is created, before any request")

    # --- Deliverables and constraints --------------------------------------------------------------------
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for title in ("## Quick start", "## Usage", "## Design decisions", "## Fixes to the provided code",
                  "## Libraries"):
        body = readme.split(title, 1)[1].split("\n## ", 1)[0] if title in readme else ""
        words = len(re.findall(r"[A-Za-z]{3,}", body))
        check("Deliverables", f"README section '{title[3:]}'", words >= 40,
              f"present in README.md with {words} words of content" if title in readme else "missing")
    broken = broken_links(["README.md"] + sorted(os.path.relpath(p, ROOT) for p in
                                                 glob.glob(os.path.join(ROOT, "docs", "*.md"))))
    check("Deliverables", "README and docs links resolve", not broken,
          "; ".join(broken[:5]) if broken else "every relative link and #anchor in README.md and docs/*.md resolves")
    check_samples(set(runs["entes"]))
    check_dependencies()
    check_cross_platform()

    counts = {status: results.count(status) for status in ("PASS", "FAIL", "SKIP")}
    print(f"\n{counts['PASS']}/{len(results)} requirements pass, {counts['FAIL']} fail, {counts['SKIP']} not verified")
    return 1 if counts["FAIL"] else 0


def requirement_names(filename: str) -> List[str]:
    names = []
    for line in open(os.path.join(ROOT, filename), encoding="utf-8"):
        line = line.strip()
        if line and line[0] not in "#-":
            names.append(normalise(re.split(r"[<>=!~ ;\[]", line)[0]))
    return names


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_samples(current_keys: set) -> None:
    """The sample results are a complete run of the current code: every record has today's fields and charts."""
    samples = os.path.join(ROOT, "sample_results")
    records = []
    for path in sorted(glob.glob(os.path.join(samples, "archive", "*.json"))):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    problems = []
    for rec in records:
        missing = current_keys - set(rec)
        if missing:
            problems.append(f"{rec['test_id']} lacks {sorted(missing)}")
        for plot in rec.get("plots", []):
            if not os.path.isfile(os.path.join(samples, "plots", os.path.basename(plot))):
                problems.append(f"missing chart {os.path.basename(plot)}")
    types = sorted(rec["ammeter_type"] for rec in records)
    if types != sorted(CLASSES):
        problems.append(f"runs for {types}, expected one per ammeter")
    for extra in ("plots/comparison.png", "allure-report.html"):
        if not os.path.isfile(os.path.join(samples, extra)):
            problems.append(f"missing {extra}")
    if not glob.glob(os.path.join(samples, "accuracy", "*.json")):
        problems.append("missing accuracy report")
    check("Deliverables", "sample test results (a complete run of the current code)", not problems,
          "; ".join(problems) if problems else
          f"{len(records)} archived runs ({', '.join(types)}) with every current result field and their charts, "
          f"comparison chart, accuracy report, Allure report")


def check_dependencies() -> None:
    """Runtime code imports only the standard library, PyYAML and matplotlib; the lock pins nothing unused."""
    sources = [os.path.join(ROOT, "main.py")] + glob.glob(os.path.join(ROOT, "Ammeters", "*.py")) \
        + glob.glob(os.path.join(ROOT, "src", "**", "*.py"), recursive=True) \
        + glob.glob(os.path.join(ROOT, "examples", "*.py"))
    imports: set = set()
    for path in sources:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    third_party = sorted(imports - set(sys.stdlib_module_names) - {"Ammeters", "src", "main", "examples"})
    runtime = requirement_names("requirements.txt")
    needed, queue = set(), runtime + requirement_names("requirements-dev.txt")
    while queue:     # everything the requirements pull in, from the installed packages' metadata
        name = normalise(queue.pop())
        if name in needed:
            continue
        needed.add(name)
        try:
            requires = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        queue += [re.split(r"[<>=!~ ;\[(]", r)[0] for r in requires if "extra ==" not in r]
    with open(os.path.join(ROOT, "requirements-lock.txt"), encoding="utf-8") as f:
        locked = [normalise(line.split("==")[0]) for line in f if "==" in line]
    unused = sorted(set(locked) - needed)
    check("Constraints", "minimize external library dependencies",
          third_party == ["matplotlib", "yaml"] and sorted(runtime) == ["matplotlib", "pyyaml"] and not unused,
          f"project code imports only {', '.join(third_party)} beyond the standard library; requirements.txt: "
          f"{', '.join(runtime)}; the lock pins {len(locked)} packages"
          + (f", {len(unused)} required by nothing: {', '.join(unused)}" if unused else ", each one required"))


def check_cross_platform() -> None:
    """Static portability rules over every Python file, plus where the suite was actually run."""
    problems = []
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(".venv"):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
                if not binary and not any(k.arg == "encoding" for k in node.keywords):
                    problems.append(f"{rel}:{node.lineno} open() without encoding")
            if isinstance(node, ast.Import):
                problems += [f"{rel}:{node.lineno} imports {a.name}" for a in node.names
                             if a.name in ("msvcrt", "winreg", "fcntl")]
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and re.search(r"\b[A-Za-z]:\\", node.value):
                problems.append(f"{rel}:{node.lineno} hard-coded Windows path")
    attributes = os.path.join(ROOT, ".gitattributes")
    if not os.path.isfile(attributes):
        problems.append(".gitattributes missing: line endings are not normalised")
    check("Constraints", "cross-platform compatibility", not problems,
          "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "") if problems else
          f"every text open() names its encoding, no OS-only modules or drive paths, line endings normalised by "
          f".gitattributes; run here on {sys.platform}; Linux via `docker compose run --rm tests`; macOS only in "
          f"the CI matrix, which runs once the repository is pushed")


class FixedPortAmmeter(EntesAmmeter):
    """ENTES with a fault injector, for the error-simulation check."""


# A stand-in device in its own process: it notes when each request arrives, on its own clock, and replies with a
# fixed reading. In the framework's process a thread's timestamps would carry GIL scheduling delays (5 ms slices)
TIMESTAMPING_DEVICE = """
import json, socket, sys, time
count = int(sys.argv[1])
with socket.socket() as s:
    s.bind(("localhost", 0)); s.listen(count)
    print(s.getsockname()[1], flush=True)
    arrivals = []
    for _ in range(count):
        conn, _ = s.accept()
        arrivals.append(time.perf_counter())
        with conn:
            conn.recv(1024); conn.sendall(b"10.0\\n")
    print(json.dumps(arrivals), flush=True)
"""


def server_side_timing(framework, hz: float = 50, count: int = 50):
    """Worst offset (ms) of request arrivals from the i / hz schedule, timed by a device in another process."""
    device = subprocess.Popen([sys.executable, "-c", TIMESTAMPING_DEVICE, str(count)], stdout=subprocess.PIPE,
                              text=True)
    try:
        assert device.stdout is not None
        port = int(device.stdout.readline())
        framework.register_ammeter("timed", port, "MEASURE")
        run = quiet(framework.run_test, "timed", {"measurements_count": count, "sampling_frequency_hz": hz,
                                                  "total_duration_seconds": None})
        arrivals = json.loads(device.stdout.readline() or "[]")
    finally:
        device.kill()
        device.wait(timeout=10)
    if len(arrivals) != count:
        return None, hz, run
    worst = max(abs(t - arrivals[0] - i / hz) for i, t in enumerate(arrivals)) * 1000
    return worst, hz, run


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def broken_links(files: List[str]) -> List[str]:
    """Relative links whose file is missing, or whose #anchor matches no heading (GitHub's slug rules)."""
    def text_of(path):
        with open(path, encoding="utf-8") as f:
            return re.sub(r"```.*?```", "", f.read(), flags=re.S)     # code blocks hold no links or headings

    problems = []
    for name in files:
        path = os.path.join(ROOT, name)
        for target in re.findall(r"\]\(([^)\s]+)\)", text_of(path)):
            if re.match(r"[a-z]+:", target):
                continue
            file_part, _, anchor = target.partition("#")
            linked = os.path.normpath(os.path.join(os.path.dirname(path), file_part)) if file_part else path
            if not os.path.exists(linked):
                problems.append(f"{name}: {target} (no such file)")
            elif anchor and linked.endswith(".md") and anchor not in {
                    _slug(h) for h in re.findall(r"^#+ (.+)$", text_of(linked), flags=re.M)}:
                problems.append(f"{name}: {target} (no such heading)")
    return problems


if __name__ == "__main__":
    sys.exit(main())
