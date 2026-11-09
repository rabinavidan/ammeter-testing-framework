import glob
import logging
import os
import platform
import threading

import allure
import pytest

from helpers import (
    EMULATOR_THREAD_PREFIX,
    TEST_CONFIG_PATH,
    FixedAmmeter,
    add_ammeter,
    free_port,
    start_emulator,
    stop_emulators,
)
from src.testing.test_framework import AmmeterTestFramework

# Allure report layout: each test module under the specification section it covers (epic) and an area (feature)
MODULE_LABELS = {
    "test_ammeter_acceptance": ("Ammeter tests (specification)", "Test runs of the three ammeters"),
    "test_emulators_and_client": ("Emulators and client", "Emulators and client"),
    "test_sampling": ("2. Measurement sampling", "Sampling and scheduling"),
    "test_statistics": ("3. Result analysis", "Statistics and control chart"),
    "test_visualization": ("3. Result analysis", "Visualization"),
    "test_archive": ("4. Result management", "Archive"),
    "test_accuracy": ("5. Accuracy assessment", "Accuracy assessment"),
    "test_error_simulation": ("Bonus: error simulation", "Error simulation"),
    "test_logger": ("Infrastructure", "Logging"),
    "test_instruments": ("5. Accuracy assessment", "Accuracy verification against a reference"),
    "test_knowledge_base": ("Infrastructure", "Knowledge base"),
    "test_config_contract": ("Infrastructure", "Configuration"),
    "test_example_smoke": ("Infrastructure", "Example script"),
    "test_framework": ("1. Unified measurement API", "Framework API and run lifecycle"),
    "test_services": ("Infrastructure", "Kubernetes services"),
    "test_ai_tooling": ("Infrastructure", "AI agent tooling (evaluation harness, gate hook)"),
}

LAYERS = ("unit", "integration", "acceptance")


def shard(config, items):
    """
    Sharding for parallel workers on separate machines (Kubernetes Indexed Job pods, CI matrix jobs): with
    PYTEST_SHARD_COUNT=N and PYTEST_SHARD_INDEX=i (or Kubernetes' JOB_COMPLETION_INDEX), keep every N-th test of
    the sorted node IDs, starting at i. Every worker collects the same tests, so the N shards are disjoint, cover
    everything, and differ in size by at most one test.
    """
    count = int(os.environ.get("PYTEST_SHARD_COUNT", "1"))
    if count <= 1:
        return
    index = int(os.environ.get("PYTEST_SHARD_INDEX", os.environ.get("JOB_COMPLETION_INDEX", "0")))
    if not 0 <= index < count:
        raise pytest.UsageError(f"shard index {index} is outside 0..{count - 1}")
    order = {nodeid: position for position, nodeid in enumerate(sorted(item.nodeid for item in items))}
    keep, drop = [], []
    for item in items:
        (keep if order[item.nodeid] % count == index else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_collection_modifyitems(config, items):
    """
    - Readable Allure titles from function names, unless a test has @allure.title. allure-pytest names a
      test from this attribute after the fixtures have run, so a title set from a fixture would be
      overwritten; a test body can still override it.
    - Layer markers from the folder: tests/unit, tests/integration, tests/acceptance. tests/unit/conftest.py
      enforces that unit tests open no sockets and start no threads.
    - `regression` on every test: the regression suite is the whole suite.
    Sharding happens later, in _Sharding, after `-m` has selected the tests.
    - Every test module must have an Allure section in MODULE_LABELS, or the run stops here.
    """
    unlabelled = sorted({item.path.stem for item in items} - set(MODULE_LABELS))
    if unlabelled:
        raise pytest.UsageError(f"Add these test modules to MODULE_LABELS in tests/conftest.py: {unlabelled}")
    for item in items:
        function = getattr(item, "obj", None)
        if function is not None and not hasattr(function, "__allure_display_name__"):
            function.__allure_display_name__ = function.__name__.removeprefix("test_").replace("_", " ").capitalize()
        parts = item.path.parts
        for layer in LAYERS:
            if layer in parts:
                item.add_marker(getattr(pytest.mark, layer))
        # The full suite is the regression; `sanity` (hand-picked) is the fast smoke subset
        item.add_marker(pytest.mark.regression)


class _Sharding:
    """
    Splits the tests across shards AFTER `-m`/`-k` selection, so shards are balanced over the tests that will
    actually run. Done in the conftest hook above (which must run before selection, as it adds the markers
    `-m` filters on), 7 sanity tests split 2 + 5 instead of 4 + 3.
    """

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        shard(config, items)


def pytest_configure(config):
    config.pluginmanager.register(_Sharding(), "ammeter-sharding")


@pytest.fixture(autouse=True)
def allure_labels(request):
    """Label every test for the Allure report by the specification section it covers, and its severity."""
    module = request.module.__name__.rsplit(".", 1)[-1]
    epic, feature = MODULE_LABELS[module]
    allure.dynamic.epic(epic)
    allure.dynamic.feature(feature)
    # Tests guarding fixes to the provided code, testing the ammeters themselves, or gating CI matter most
    critical = any(request.node.get_closest_marker(m) for m in ("bugfix", "acceptance", "sanity"))
    allure.dynamic.severity(allure.severity_level.CRITICAL if critical else allure.severity_level.NORMAL)


def attach_run_output(directory) -> None:
    """Attach the charts and run logs a test produced under `directory`/results to its Allure entry."""
    for path in sorted(glob.glob(os.path.join(directory, "results", "plots", "*.png"))):
        allure.attach.file(path, name=os.path.basename(path), attachment_type=allure.attachment_type.PNG)
    for path in sorted(glob.glob(os.path.join(directory, "results", "logs", "*.log"))):
        allure.attach.file(path, name=os.path.basename(path), attachment_type=allure.attachment_type.TEXT)


@pytest.fixture
def emulators():
    """
    Start emulators for one test: call it with an emulator, get its port. Every emulator it started is
    stopped and its thread joined when the test ends, so nothing leaks into later tests.
    """
    running = []

    def start(emulator) -> int:
        running.append((emulator, start_emulator(emulator)))
        return emulator.port

    yield start
    stop_emulators(running)


@pytest.fixture
def framework(tmp_path, monkeypatch):
    """
    A framework using tests/config/test_config.yaml (request timeout 0.5 s, charts off), with all output
    (logs, archive, plots, accuracy reports) going to a temporary directory. Charts and logs the test
    produced are attached to the Allure report.
    """
    with allure.step("Create the framework from tests/config/test_config.yaml, writing output to a temporary "
                     "folder (request timeout 0.5 s, charts off)"):
        monkeypatch.chdir(tmp_path)
        fw = AmmeterTestFramework(TEST_CONFIG_PATH)
    yield fw
    close_test_loggers_now()
    with allure.step("Attach the charts and run logs this test produced"):
        attach_run_output(str(tmp_path))


@pytest.fixture
def fixed_ammeter(framework, emulators):
    """A FixedAmmeter (10 A) registered in the framework as 'fixed'; returns the emulator."""
    emulator = FixedAmmeter(free_port())
    emulators(emulator)
    add_ammeter(framework, "fixed", emulator.port)
    return emulator


def close_test_loggers_now() -> None:
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("test_") and isinstance(logger, logging.Logger):
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def close_test_loggers():
    """Close the per-run log files so the temporary directories can be removed (Windows)."""
    yield
    close_test_loggers_now()


@pytest.fixture(scope="session", autouse=True)
def no_leaked_emulators():
    """Fail the run if any emulator thread is still alive at the end: a test didn't stop what it started."""
    yield
    leaked = [t.name for t in threading.enumerate() if t.name.startswith(EMULATOR_THREAD_PREFIX) and t.is_alive()]
    assert leaked == [], f"emulator threads still running after the session: {leaked}"


def pytest_sessionfinish(session):
    """Describe the test environment in the Allure report (shown on its overview page)."""
    results_dir = session.config.getoption("allure_report_dir", None)
    if not results_dir:
        return
    import matplotlib
    import yaml
    os.makedirs(results_dir, exist_ok=True)
    environment = {
        "Python": platform.python_version(),
        "Platform": platform.platform(),
        "matplotlib": matplotlib.__version__,
        "PyYAML": yaml.__version__,
        "pytest": pytest.__version__,
        # Forward slashes: a backslash is an escape character in .properties files
        "Config": os.path.relpath(TEST_CONFIG_PATH).replace("\\", "/"),
    }
    with open(os.path.join(results_dir, "environment.properties"), "w", encoding="utf-8") as f:
        for key, value in environment.items():
            f.write(f"{key}={value}\n")
