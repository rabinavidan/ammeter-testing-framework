import copy
import math
import random
import threading
import time
from typing import Any, Dict, Optional

from Ammeters.client import TERMINATOR

# Every fault and its parameters. A fault with probability 0 (or a zero size) is off.
#
# Transport faults - the reading is lost; at most one per request:
#   no_response         accept, then hang for delay_seconds without replying (client times out)
#   dropped_connection  close the connection without replying (client gets no data)
#   corrupted_response  reply with a line that isn't a usable reading ("NaN", "inf", invalid UTF-8, ...)
#   truncated_response  send the first part of the reading, then close before the terminator
# Delivery faults - the reading still arrives intact:
#   latency             reply late by delay_seconds (still within the client timeout)
#   fragmented_response send the reply in two pieces gap_seconds apart (the client must reassemble it)
# Measurement faults - the reply is a valid number, but wrong:
#   offset              systematic calibration error: reading * (1 + relative)
#   drift               error that grows with each reading: reading * (1 + relative_per_reading * n)
#   noise               random error: reading * (1 + gauss(0, relative_sd))
#   spike               occasional outlier: reading * factor
#   mains_ripple        interference from the mains: reading * (1 + amplitude_relative * sin(2π f t)),
#                       t = time since the simulator was created; sampled slowly, it aliases
DEFAULT_FAULTS: Dict[str, Dict[str, float]] = {
    "no_response": {"probability": 0.0, "delay_seconds": 3.0},
    "dropped_connection": {"probability": 0.0},
    "corrupted_response": {"probability": 0.0},
    "truncated_response": {"probability": 0.0},
    "latency": {"probability": 0.0, "delay_seconds": 0.05},
    "fragmented_response": {"probability": 0.0, "gap_seconds": 0.05},
    "offset": {"relative": 0.0},
    "drift": {"relative_per_reading": 0.0},
    "noise": {"relative_sd": 0.0},
    "spike": {"probability": 0.0, "factor": 10.0},
    "mains_ripple": {"amplitude_relative": 0.0, "frequency_hz": 50.0},
}

TRANSPORT_FAULTS = ("no_response", "dropped_connection", "corrupted_response", "truncated_response")

CORRUPTED_PAYLOADS = [b"NaN", b"inf", b"\xff\xfe\x00", b"ERR", b"12.3.4", b""]


def _merge_faults(*layers: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, float]]:
    merged = copy.deepcopy(DEFAULT_FAULTS)
    for layer in layers:
        for name, params in (layer or {}).items():
            if name not in DEFAULT_FAULTS:
                raise ValueError(f"Unknown fault {name!r}. Available: {list(DEFAULT_FAULTS)}")
            for key, value in (params or {}).items():
                if key not in DEFAULT_FAULTS[name]:
                    raise ValueError(f"Unknown parameter {key!r} for fault {name!r}. "
                                     f"Available: {list(DEFAULT_FAULTS[name])}")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{name}.{key} must be a number, got {value!r}")
                merged[name][key] = float(value)
    for name, params in merged.items():
        probability = params.get("probability")
        if probability is not None and not 0 <= probability <= 1:
            raise ValueError(f"{name}.probability must be between 0 and 1, got {probability}")
        for key in ("delay_seconds", "relative_sd", "gap_seconds", "amplitude_relative", "frequency_hz"):
            if params.get(key, 0) < 0:
                raise ValueError(f"{name}.{key} must not be negative, got {params[key]}")
    return merged


class ErrorSimulator:
    """
    Injects faults into an ammeter emulator's replies, so the test framework meets
    the failures a real device produces (timeouts, dropped and garbled replies,
    wrong readings) over the real socket.

    Each emulator gets its own instance. `counts()` reports what was injected, to
    check against what the framework detected.
    """

    def __init__(self, faults: Optional[Dict[str, Dict[str, Any]]] = None, seed: Any = None):
        self.faults = _merge_faults(faults)
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._readings = 0
        self._started = time.monotonic()     # time origin for mains_ripple
        self._counts = {"requests": 0, **{name: 0 for name in DEFAULT_FAULTS}}

    @classmethod
    def from_config(cls, config: Dict, ammeter_type: str) -> Optional["ErrorSimulator"]:
        """
        Build the simulator for one ammeter from the config's `error_simulation`
        section, or return None if simulation is disabled. Per-ammeter settings in
        `error_simulation.ammeters.<type>` are merged over the shared `faults`.
        """
        sim_config = config.get("error_simulation") or {}
        if not sim_config.get("enabled", False):
            return None
        faults = _merge_faults(sim_config.get("faults"), (sim_config.get("ammeters") or {}).get(ammeter_type))

        # A hang shorter than the client timeout would look like latency, not a lost reply
        timeout = (config.get("testing") or {}).get("request_timeout_seconds")
        hang = faults["no_response"]
        if hang["probability"] > 0 and timeout is not None and hang["delay_seconds"] <= timeout:
            raise ValueError(f"error_simulation no_response.delay_seconds ({hang['delay_seconds']:g}) must be "
                             f"longer than testing.request_timeout_seconds ({timeout:g})")

        seed = sim_config.get("seed")
        # Same seed, different stream per ammeter (string seeds are deterministic)
        return cls(faults, seed=None if seed is None else f"{seed}-{ammeter_type}")

    def counts(self) -> Dict[str, int]:
        """How many requests were served and how many times each fault was injected."""
        with self._lock:
            return dict(self._counts)

    def _fires(self, name: str) -> bool:
        return self._rng.random() < self.faults[name]["probability"]

    def respond(self, conn, current: float) -> None:
        """Send `current` over `conn`, applying whichever faults fire for this request."""
        with self._lock:
            self._counts["requests"] += 1
            transport = next((name for name in TRANSPORT_FAULTS if self._fires(name)), None)
            late = transport is None and self._fires("latency")
            fragmented = transport is None and self._fires("fragmented_response")
            if transport is None:
                current, applied = self._distort(current)
            else:
                applied = []
            delivery = ["latency" if late else None, "fragmented_response" if fragmented else None]
            self._counts.update({name: self._counts[name] + 1 for name in applied + [transport] + delivery if name})
            payload = self._rng.choice(CORRUPTED_PAYLOADS) if transport == "corrupted_response" else None

        reply = str(current).encode("utf-8")
        if transport == "no_response":
            print(f"[error simulation] no_response: hanging {self.faults['no_response']['delay_seconds']:g}s")
            time.sleep(self.faults["no_response"]["delay_seconds"])
            return
        if transport == "dropped_connection":
            print("[error simulation] dropped_connection")
            return
        if transport == "corrupted_response":
            print(f"[error simulation] corrupted_response: {payload!r}")
            assert payload is not None
            conn.sendall(payload + TERMINATOR)
            return
        if transport == "truncated_response":
            # The first part of a valid reading: parsed on its own it would be a wrong number
            cut = reply[:max(1, len(reply) // 2)]
            print(f"[error simulation] truncated_response: {cut!r} of {reply!r}")
            conn.sendall(cut)
            return
        if late:
            time.sleep(self.faults["latency"]["delay_seconds"])
        if fragmented:
            middle = max(1, len(reply) // 2)
            conn.sendall(reply[:middle])
            time.sleep(self.faults["fragmented_response"]["gap_seconds"])
            conn.sendall(reply[middle:] + TERMINATOR)
            return
        conn.sendall(reply + TERMINATOR)

    def _distort(self, current: float):
        """Apply the measurement faults; returns (value, names of faults that changed it)."""
        applied = []
        offset = self.faults["offset"]["relative"]
        if offset:
            current *= 1 + offset
            applied.append("offset")
        drift = self.faults["drift"]["relative_per_reading"]
        if drift:
            current *= 1 + drift * self._readings
            applied.append("drift")
        self._readings += 1
        noise = self.faults["noise"]["relative_sd"]
        if noise:
            current *= 1 + self._rng.gauss(0, noise)
            applied.append("noise")
        if self._fires("spike"):
            current *= self.faults["spike"]["factor"]
            applied.append("spike")
        ripple = self.faults["mains_ripple"]
        if ripple["amplitude_relative"]:
            t = time.monotonic() - self._started
            current *= 1 + ripple["amplitude_relative"] * math.sin(2 * math.pi * ripple["frequency_hz"] * t)
            applied.append("mains_ripple")
        return current, applied
