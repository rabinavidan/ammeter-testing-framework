import allure
import pytest

from Ammeters.client import TERMINATOR
from Ammeters.error_simulation import CORRUPTED_PAYLOADS, ErrorSimulator
from helpers import FakeConnection, attach_json


def config(sim, timeout=2.0):
    return {"testing": {"request_timeout_seconds": timeout}, "error_simulation": {"enabled": True, **sim}}


def respond_once(faults, value=10.0, seed=1):
    """Send one reply through a simulator with `faults`; return what went on the wire and the counts."""
    simulator = ErrorSimulator(faults, seed=seed)
    connection = FakeConnection()
    simulator.respond(connection, value)
    return connection, simulator.counts()


def _part_each_fault_puts_the_right_thing_on_the_wire():
    """What respond() sends for no fault, each lost-reply fault, delivery faults and measurement faults."""
    with allure.step("No faults: one line b'10.0\\n'; one request counted"):
        connection, counts = respond_once({})
        assert connection.sent == [b"10.0" + TERMINATOR] and counts["requests"] == 1
    for fault, extra, expected in [("dropped_connection", {}, b""), ("no_response", {"delay_seconds": 0}, b""),
                                   ("truncated_response", {}, b"12.")]:
        with allure.step(f"{fault}: sends {expected!r} (nothing usable) and is counted"):
            connection, counts = respond_once({fault: {"probability": 1, **extra}}, value=12.345)
            assert connection.data == expected and counts[fault] == 1
    with allure.step("corrupted_response: a properly terminated line that isn't a reading"):
        connection, counts = respond_once({"corrupted_response": {"probability": 1}})
        assert connection.data.endswith(TERMINATOR) and connection.data[:-1] in CORRUPTED_PAYLOADS
    with allure.step("fragmented_response: two sends that join into exactly b'12.345\\n'"):
        connection, _ = respond_once({"fragmented_response": {"probability": 1, "gap_seconds": 0}}, 12.345)
        assert len(connection.sent) == 2 and connection.data == b"12.345" + TERMINATOR
    for faults, expected in [({"offset": {"relative": 0.05}}, 10.5),
                             ({"spike": {"probability": 1, "factor": 10}}, 100.0),
                             ({"drift": {"relative_per_reading": 0.5}}, 10.0)]:
        with allure.step(f"{faults}: a valid but wrong reading, {expected}"):
            connection, _ = respond_once(faults)
            assert float(connection.data) == pytest.approx(expected)


def _part_fault_configuration():
    """Off unless enabled; shared faults plus per-ammeter ones; seeded patterns repeat; bad settings rejected."""
    with allure.step("Disabled, or no section at all: no simulator"):
        assert ErrorSimulator.from_config({"error_simulation": {"enabled": False}}, "entes") is None
        assert ErrorSimulator.from_config({}, "entes") is None
    cfg = config({"seed": 0, "faults": {"dropped_connection": {"probability": 0.5}},
                  "ammeters": {"entes": {"offset": {"relative": 0.05}}}})
    attach_json("error_simulation config", cfg["error_simulation"])
    with allure.step("ENTES gets the shared drops and its own 5 % offset; Greenlee only the shared drops"):
        entes, greenlee = ErrorSimulator.from_config(cfg, "entes"), ErrorSimulator.from_config(cfg, "greenlee")
        assert entes.faults["offset"]["relative"] == 0.05 and entes.faults["dropped_connection"]["probability"] == 0.5
        assert greenlee.faults["offset"]["relative"] == 0
        assert greenlee.faults["dropped_connection"]["probability"] == 0.5

    def dropped(name):
        simulator, pattern = ErrorSimulator.from_config(cfg, name), []
        for _ in range(30):
            connection = FakeConnection()
            simulator.respond(connection, 10.0)
            pattern.append(connection.data == b"")
        return pattern

    with allure.step("Seed 0: two ENTES simulators drop the same 30 requests; Greenlee drops different ones"):
        assert dropped("entes") == dropped("entes") != dropped("greenlee")
    for sim, message in [({"faults": {"meltdown": {}}}, "Unknown fault"),
                         ({"faults": {"spike": {"size": 2}}}, "Unknown parameter"),
                         ({"faults": {"spike": {"probability": 1.5}}}, "between 0 and 1"),
                         ({"faults": {"latency": {"delay_seconds": -1}}}, "must not be negative"),
                         ({"faults": {"noise": {"relative_sd": "big"}}}, "must be a number"),
                         ({"faults": {"no_response": {"probability": 0.1, "delay_seconds": 1.0}}}, "longer than")]:
        with allure.step(f"{sim['faults']} -> ValueError '{message}'"):
            with pytest.raises(ValueError, match=message):
                ErrorSimulator.from_config(config(sim), "entes")


def test_error_simulation():
    """Each fault puts the right bytes on the wire, and fault configuration is validated."""
    with allure.step(
            "What respond() sends for no fault, each lost-reply fault, delivery faults and measurement faults."):
        _part_each_fault_puts_the_right_thing_on_the_wire()
    with allure.step(
            "Off unless enabled; shared faults plus per-ammeter ones; seeded patterns repeat; bad settings "
            "rejected."):
        _part_fault_configuration()
