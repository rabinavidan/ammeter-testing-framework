import socket
import threading

import pytest

REFUSED = ("unit tests must not open sockets or start threads: this test belongs in tests/integration "
           "(or use the fakes in tests/helpers.py: FakeClock, FakeConnection, a fake transport)")


@pytest.fixture(autouse=True)
def no_sockets_or_threads(monkeypatch):
    """Enforce the unit layer's boundary: no network, no background threads, so unit tests stay fast and exact."""
    def refuse(*args, **kwargs):
        raise AssertionError(REFUSED)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "bind", refuse)
    monkeypatch.setattr(threading.Thread, "start", refuse)
