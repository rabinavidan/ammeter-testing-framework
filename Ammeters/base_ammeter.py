import random
import socket
import threading
from abc import ABC, abstractmethod

from Ammeters.client import TERMINATOR

NotImplementedErrorMsg = "Subclasses must implement this property."

# How often the accept loop checks for stop(), and how long a connected client may stay silent
ACCEPT_POLL_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 5.0

class AmmeterEmulatorBase(ABC):
    def __init__(self, port: int, error_simulator=None, instrument=None, seed=None, host: str = "localhost"):
        self.port = port
        # Interface to listen on: localhost by default; 0.0.0.0 in a container that other pods must reach
        self.host = host
        # Optional Ammeters.error_simulation.ErrorSimulator; None = always reply correctly
        self.error_simulator = error_simulator
        # Optional Ammeters.instrument_model.InstrumentModel: reply from a realistic meter measuring a
        # reference current instead of from measure_current(); None = the original formula
        self.instrument = instrument
        if error_simulator is not None and instrument is not None:
            raise ValueError("Use an error simulator or an instrument model, not both")
        # Each emulator has its own random generator. The provided code reseeded Python's global
        # generator from the clock on every instance, which disturbed every other user of `random`
        # and made runs impossible to reproduce. seed=None still gives a fresh sequence each time.
        self.rng = random.Random(seed)
        self.ready = threading.Event()      # set once the server is listening
        self._stop = threading.Event()

    def stop(self):
        """Ask start_server() to return; it does within ACCEPT_POLL_SECONDS (or after a request in progress)."""
        self._stop.set()

    def start_server(self):
        """
        Starts the server to listen for client requests.
        The server runs until stop() is called, handling one client request at a time.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, self.port))
            s.listen()
            # Wake up regularly to notice stop(); accepted connections are blocking
            s.settimeout(ACCEPT_POLL_SECONDS)
            print(f"{self.__class__.__name__} is running on port {self.port}")
            self.ready.set()
            while not self._stop.is_set():
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                conn.settimeout(REQUEST_TIMEOUT_SECONDS)
                with conn:
                    print(f"Connected by {addr}")
                    try:
                        data = conn.recv(1024)
                        if data == self.get_current_command:
                            # Every reply is one line, ended by TERMINATOR, so the client can tell
                            # a complete reply from one cut off in transit
                            if self.instrument is not None:
                                conn.sendall(self.instrument.read_reply().encode('utf-8') + TERMINATOR)
                                continue
                            # Call the specific measure_current() method defined in subclasses
                            current = self.measure_current()
                            if self.error_simulator is None:
                                conn.sendall(str(current).encode('utf-8') + TERMINATOR)
                            else:
                                self.error_simulator.respond(conn, current)
                    except OSError as e:
                        # A client that disconnects mid-request must not take the server down
                        print(f"{self.__class__.__name__}: connection from {addr} failed: {e}")

    @property
    @abstractmethod
    def get_current_command(self) -> bytes:
        """
        This property must be implemented by each subclass to provide the specific
        command to get the current measurement.
        """
        raise NotImplementedError(NotImplementedErrorMsg)

    @abstractmethod
    def measure_current(self) -> float:
        """
        This method must be implemented by each subclass to provide the specific
        logic for current measurement.
        """
        raise NotImplementedError(NotImplementedErrorMsg)

