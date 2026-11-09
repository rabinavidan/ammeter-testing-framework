import json
import os
import pathlib
import socket
import threading

import allure

from Ammeters.base_ammeter import AmmeterEmulatorBase

REPO = pathlib.Path(__file__).resolve().parent.parent
# Tests use their own config; the production config is only read by its contract test
TEST_CONFIG_PATH = str(REPO / "tests" / "config" / "test_config.yaml")
PRODUCTION_CONFIG_PATH = str(REPO / "config" / "config.yaml")
K8S_CONFIG_PATH = str(REPO / "deploy" / "k8s" / "base" / "config.yaml")

# Fast sampling for tests that only need a few readings
FAST = {"measurements_count": 5, "sampling_frequency_hz": 50, "total_duration_seconds": None}

EMULATOR_THREAD_PREFIX = "emulator-"


class FixedAmmeter(AmmeterEmulatorBase):
    """Always reads `value`, so the effect of a fault or a statistic can be checked exactly."""

    def __init__(self, port: int, error_simulator=None, value: float = 10.0):
        super().__init__(port, error_simulator)
        self.value = value

    @property
    def get_current_command(self) -> bytes:
        return b"MEASURE_FIXED"

    def measure_current(self) -> float:
        return self.value


class FakeClock:
    """Virtual time for the framework: sleep() advances now() instantly, and every call is recorded."""

    def __init__(self):
        self.time = 0.0
        self.sleeps = []

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


class FakeConnection:
    """Stands in for a socket when testing ErrorSimulator.respond: records what would be sent."""

    def __init__(self):
        self.sent = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    @property
    def data(self) -> bytes:
        return b"".join(self.sent)


# Each parallel worker (pytest-xdist sets PYTEST_XDIST_WORKER=gw0, gw1, ...) takes ports from its own block, below
# the OS's dynamic range (49152+ on Windows, 32768+ on Linux), so no other worker and no OS-assigned port can be
# handed the same number between free_port() and the emulator's bind. Asking the OS for port 0 could not promise that.
_PORT_BLOCK = 400
_next_port = [0]


def free_port() -> int:
    """A port nothing is listening on, unique to this test process."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    base = 20000 + int(worker.removeprefix("gw") or 0) % 40 * _PORT_BLOCK
    for _ in range(_PORT_BLOCK):
        port = base + _next_port[0] % _PORT_BLOCK
        _next_port[0] += 1
        with socket.socket() as s:
            try:
                s.bind(("localhost", port))
            except OSError:
                continue        # taken by something outside the suite
            return port
    raise RuntimeError(f"no free port in {base}-{base + _PORT_BLOCK - 1}")


def start_emulator(emulator: AmmeterEmulatorBase) -> threading.Thread:
    """
    Run an emulator in a named daemon thread and wait until it is listening. Callers must
    stop() it and join the thread; the `emulators` fixture does that.
    """
    name = type(emulator).__name__
    extra = f" reading {emulator.value} A" if isinstance(emulator, FixedAmmeter) else ""
    faults = " with error simulation" if emulator.error_simulator is not None else ""
    model = " answering from an instrument model" if emulator.instrument is not None else ""
    with allure.step(f"Start {name}{extra}{faults}{model} on port {emulator.port}"):
        thread = threading.Thread(target=emulator.start_server, daemon=True,
                                  name=f"{EMULATOR_THREAD_PREFIX}{name}-{emulator.port}")
        thread.start()
        if not emulator.ready.wait(5):
            raise RuntimeError(f"Emulator on port {emulator.port} did not start")
        return thread


def stop_emulators(running) -> None:
    """Stop emulators started with start_emulator and wait for their threads to end."""
    for emulator, _ in running:
        emulator.stop()
    for _, thread in running:
        # A simulated hang can keep a server busy for a few seconds before it notices stop()
        thread.join(timeout=10)


def add_ammeter(framework, name: str, port: int, command: str = "MEASURE_FIXED", plausible_range_a=None) -> None:
    with allure.step(f"Register ammeter '{name}': port {port}, command '{command}'"
                     + (f", plausible range {plausible_range_a}" if plausible_range_a else "")):
        framework.register_ammeter(name, port, command, plausible_range_a)


def run(framework, ammeter_type: str, sampling=None) -> dict:
    """framework.run_test as a report step, with the result attached."""
    label = describe_sampling(sampling) if sampling else "the configured sampling"
    with allure.step(f"Run test on '{ammeter_type}' with {label}"):
        result = framework.run_test(ammeter_type, sampling)
        attach_json("run result", {key: result[key] for key in (
            "test_id", "sampling", "actual_duration_seconds", "values", "errors", "error_counts", "statistics")})
        return result


def describe_sampling(sampling: dict) -> str:
    parts = []
    if sampling.get("measurements_count") is not None:
        parts.append(f"{sampling['measurements_count']} readings")
    if sampling.get("sampling_frequency_hz") is not None:
        parts.append(f"at {sampling['sampling_frequency_hz']:g} Hz")
    if sampling.get("total_duration_seconds") is not None:
        parts.append(f"over {sampling['total_duration_seconds']:g} s")
    return " ".join(parts) or "no sampling values"


def make_result(ammeter_type: str, values, test_id: str = None) -> dict:
    """A minimal run_test-shaped result, for testing analysis code without sampling."""
    return {
        "test_id": test_id or f"{ammeter_type}_00000000",
        "ammeter_type": ammeter_type,
        "values": list(values),
        "sampling": {"measurements_count": len(values)},
    }


def attach_json(name: str, data) -> None:
    allure.attach(json.dumps(data, indent=2, default=str), name=name, attachment_type=allure.attachment_type.JSON)
