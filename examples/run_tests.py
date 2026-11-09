import argparse
import io
import sys
import threading
from typing import Callable, Dict, Optional

from Ammeters.base_ammeter import AmmeterEmulatorBase
from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.error_simulation import TRANSPORT_FAULTS, ErrorSimulator
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from src.analysis.accuracy import format_accuracy_report
from src.testing.test_framework import AmmeterTestFramework

EMULATOR_CLASSES: Dict[str, Callable[..., AmmeterEmulatorBase]] = {
    "greenlee": GreenleeAmmeter, "entes": EntesAmmeter, "circutor": CircutorAmmeter}


def main(config_path: str = "config/config.yaml", ports: Optional[Dict[str, int]] = None,
         start_emulators: bool = True) -> Dict[str, dict]:
    """
    Test every ammeter in the config and print the results, comparison, accuracy report and archive
    history. Ammeters with an emulator class (EMULATOR_CLASSES) are started here; any other configured
    ammeter must already be listening at its host and port. `ports` overrides the configured ports (e.g.
    free ports in CI). With start_emulators=False it starts nothing and tests ammeters already running
    at the configured hosts and ports (deployed emulator Services, or real devices). Returns the results.
    """
    # Create the test framework
    framework = AmmeterTestFramework(config_path)
    if ports:
        framework.configure({"ammeters": {name: {"port": port} for name, port in ports.items()}})
    ammeter_types = list(framework.config.get("ammeters") or {})

    # Start the emulators, with fault injection if the config enables it
    simulators = {t: ErrorSimulator.from_config(framework.config, t) for t in ammeter_types}
    emulators = {t: EMULATOR_CLASSES[t](framework.config["ammeters"][t]["port"], simulators[t])
                 for t in ammeter_types if t in EMULATOR_CLASSES} if start_emulators else {}
    for t in ammeter_types:
        if start_emulators and t not in emulators:
            print(f"{t}: no emulator class; testing the device already at its configured host and port")
    threads = [threading.Thread(target=e.start_server, daemon=True) for e in emulators.values()]
    for thread in threads:
        thread.start()
    for emulator in emulators.values():
        emulator.ready.wait(5)

    try:
        # Run a test on every ammeter type
        results = {}
        for ammeter_type in ammeter_types:
            print(f"Testing {ammeter_type} ammeter...")
            results[ammeter_type] = framework.run_test(ammeter_type)
    finally:
        for emulator in emulators.values():
            emulator.stop()
        for thread in threads:
            thread.join(timeout=10)

    # Print each run's results
    for ammeter_type, result in results.items():
        print(f"\nResults for {ammeter_type}:")
        print(f"  Test ID: {result['test_id']}")
        print(f"  Samples: {len(result['values'])}/{result['sampling']['measurements_count']} "
              f"in {result['actual_duration_seconds']:.3f} s, errors: {len(result['errors'])}")
        print(f"  Values: {[round(v, 4) for v in result['values']]}")
        for name, value in result["statistics"].items():
            print(f"  {name}: {value:.6g}" if value is not None else f"  {name}: n/a")
        print(f"  Archived to: {result['archive_path']}")
        print(f"  Plots: {result['plots']}")
        if result["errors"]:
            print(f"  Errors detected: {result['error_counts']}")
        simulator = simulators[ammeter_type] if ammeter_type in emulators else None
        if simulator is not None:
            injected = {name: n for name, n in simulator.counts().items() if n}
            lost = sum(injected.get(name, 0) for name in TRANSPORT_FAULTS)
            # Readings outside plausible_range_a (e.g. a x10 spike) arrived but were rejected: count them apart
            implausible = result["error_counts"].get("ImplausibleReadingError", 0)
            print(f"  Faults injected: {injected}")
            failed = len(result["errors"]) - implausible
            print(f"  Lost-reply faults injected: {lost}, failed readings detected: {failed}, "
                  f"implausible readings rejected: {implausible}")

    # Comparison chart of all the ammeters
    print(f"\nComparison plot: {framework.plot_comparison(list(results.values()))}")

    # Accuracy (precision) assessment across the ammeter types
    assessment = framework.assess_accuracy(list(results.values()))
    print("\n" + format_accuracy_report(assessment))
    print(f"\nAccuracy report saved to: {assessment['report_path']}")

    # Retrieve history from the archive and compare
    archive = framework.archive
    print(f"\nArchive {archive.archive_dir}: {len(archive.list_runs())} runs")
    for ammeter_type in ammeter_types:
        history = archive.list_runs(ammeter_type)
        latest_ids = [run["test_id"] for run in history[-3:]]
        print(f"\nLast {len(latest_ids)} {ammeter_type} runs:")
        for test_id, row in archive.compare(latest_ids, ["mean", "std_dev", "coefficient_of_variation"]).items():
            print(f"  {test_id}  {row['start_time']}  " + "  ".join(
                f"{k}={v:.4g}" if v is not None else f"{k}=n/a"
                for k, v in row.items() if k not in ("ammeter_type", "start_time")
            ))
    return results


if __name__ == "__main__":
    # A console that can't encode a character (e.g. an ASCII or cp1252 pipe) shows '?' instead of ending the run
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Test the three ammeter emulators end to end.")
    parser.add_argument("--config", default="config/config.yaml", help="config file (default config/config.yaml)")
    parser.add_argument("--ports", type=int, nargs=3, metavar=("GREENLEE", "ENTES", "CIRCUTOR"),
                        help="override the configured ports")
    parser.add_argument("--no-emulators", action="store_true",
                        help="don't start emulators: test the ammeters already running at the configured hosts")
    parser.add_argument("--fail-on-errors", action="store_true",
                        help="exit 1 if any reading failed (for health checks)")
    args = parser.parse_args()
    results = main(args.config, dict(zip(EMULATOR_CLASSES, args.ports, strict=True)) if args.ports else None,
                   start_emulators=not args.no_emulators)
    if args.fail_on_errors and any(result["errors"] for result in results.values()):
        raise SystemExit(1)
