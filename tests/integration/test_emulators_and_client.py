import socket
import struct
import threading
import time

import allure
import pytest

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.client import TERMINATOR, OverRangeError, TruncatedReplyError, request_current_from_ammeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.error_simulation import ErrorSimulator
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from helpers import FixedAmmeter, free_port, start_emulator

# Emulator, README command, and the range its formula can produce
EMULATORS = [
    (GreenleeAmmeter, b"MEASURE_GREENLEE -get_measurement", 1.0 / 100.0, 10.0 / 0.1),
    (EntesAmmeter, b"MEASURE_ENTES -get_data", 0.01 * 500, 0.1 * 2000),
    (CircutorAmmeter, b"MEASURE_CIRCUTOR -get_measurement", 10 * 0.1 * 0.001, 10 * 1.0 * 0.01),
]


def raw_reply(port: int, command: bytes) -> bytes:
    """Everything the emulator sends until it closes the connection."""
    with socket.create_connection(("localhost", port), timeout=2) as s:
        s.sendall(command)
        data = b""
        while chunk := s.recv(1024):
            data += chunk
        return data


def serve_once(chunks):
    """A one-shot fake device that sends `chunks` (bytes, or a float = pause in seconds) and closes."""
    server = socket.socket()
    server.bind(("localhost", 0))
    server.listen()
    port = server.getsockname()[1]

    def handle():
        conn, _ = server.accept()
        with conn:
            conn.recv(1024)
            for chunk in chunks:
                time.sleep(chunk) if isinstance(chunk, float) else conn.sendall(chunk)
        server.close()

    threading.Thread(target=handle, daemon=True).start()
    return port


def _part_each_emulator_speaks_the_protocol(emulators):
    """Each emulator answers its README command with one terminated line in its formula's range, and ignores others."""
    for cls, command, low, high in EMULATORS:
        port = emulators(cls(free_port()))
        with allure.step(f"{cls.__name__}: 10 replies to {command.decode()!r}, all within {low:g}..{high:g} A"):
            values = [request_current_from_ammeter(port, command, timeout=2) for _ in range(10)]
            assert all(low <= v <= high for v in values)
        with allure.step(f"{cls.__name__}: the raw reply is exactly one line ending in the terminator"):
            data = raw_reply(port, command)
            assert data.endswith(TERMINATOR) and data.count(TERMINATOR) == 1
        with allure.step(f"{cls.__name__}: any other command gets no reply (None)"):
            assert request_current_from_ammeter(port, b"MEASURE_SOMETHING_ELSE", timeout=2) is None


def _part_the_client_reads_whole_replies_and_rejects_partial_ones(capsys):
    """Regression (single recv): a reply split in transit is reassembled; a cut-off one or 'OL' is an error."""
    with allure.step("'12.' then, 0.1 s later, '345\\n' -> 12.345 (the old client read 12.0)"):
        assert request_current_from_ammeter(serve_once([b"12.", 0.1, b"345" + TERMINATOR]), b"X", timeout=2) == 12.345
        assert "measurement from port" in capsys.readouterr().out
    with allure.step("A non-UTF-8 reply raises UnicodeDecodeError and is printed as plain ASCII, not as a reading"):
        with pytest.raises(UnicodeDecodeError):
            request_current_from_ammeter(serve_once([b"\xff" + TERMINATOR]), b"X", timeout=2)
        out = capsys.readouterr().out
        assert "Invalid reply" in out and r"b'\xff\n'" in out and out.isascii()
    with allure.step("'12.3' then the connection closes -> TruncatedReplyError, never 12.3"):
        with pytest.raises(TruncatedReplyError):
            request_current_from_ammeter(serve_once([b"12.3"]), b"X", timeout=2)
    with allure.step("'OL\\n' -> OverRangeError"):
        with pytest.raises(OverRangeError):
            request_current_from_ammeter(serve_once([b"OL" + TERMINATOR]), b"X", timeout=2)


@pytest.mark.sanity
@pytest.mark.bugfix
def test_the_emulators_and_client_over_real_sockets(emulators, capsys):
    """Each emulator speaks the protocol; the client reassembles split replies and rejects partial ones."""
    with allure.step(
            "Each emulator answers its README command with one terminated line in its formula's range, and "
            "ignores others."):
        _part_each_emulator_speaks_the_protocol(emulators)
    with allure.step(
            "Regression (single recv): a reply split in transit is reassembled; a cut-off one or 'OL' is an "
            "error."):
        _part_the_client_reads_whole_replies_and_rejects_partial_ones(capsys)


@pytest.mark.bugfix
@pytest.mark.timing
def test_the_client_never_hangs_and_the_server_never_dies(emulators):
    """Regressions: no client timeout; a client reset killed the server. Also: stop() frees the port."""
    hanging = ErrorSimulator({"no_response": {"probability": 1, "delay_seconds": 1.0}})
    port = emulators(FixedAmmeter(free_port(), hanging))
    with allure.step("A device that hangs 1 s: the 0.3 s client timeout raises TimeoutError well before that"):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            request_current_from_ammeter(port, b"MEASURE_FIXED", timeout=0.3)
        assert time.monotonic() - started < 0.9
    slow = ErrorSimulator({"latency": {"probability": 1, "delay_seconds": 0.3}})
    port = emulators(FixedAmmeter(free_port(), slow))
    with allure.step("A client resets the connection before the late reply; the server still answers next time"):
        client = socket.create_connection(("localhost", port))
        client.sendall(b"MEASURE_FIXED")
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))     # close = reset
        client.close()
        time.sleep(0.5)
        slow.faults["latency"]["probability"] = 0
        assert request_current_from_ammeter(port, b"MEASURE_FIXED", timeout=2) == 10.0
    with allure.step("stop() ends the server within 1 s and frees its port"):
        emulator = FixedAmmeter(free_port())
        thread = start_emulator(emulator)
        emulator.stop()
        thread.join(timeout=1)
        assert not thread.is_alive()
        with socket.socket() as s:
            s.bind(("localhost", emulator.port))
