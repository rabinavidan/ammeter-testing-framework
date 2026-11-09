"""
Realistic instrument behaviour for accuracy testing (opt-in).

The original emulators draw a fresh random current from their formula on every request, so there is
no true current to compare against. Here a ReferenceSource plays the calibrator: it holds the true
current. An InstrumentModel measures it the way a real meter does: with a fixed gain error and
offset, an absolute noise floor plus noise proportional to the current, quantized to its resolution,
limited to its range ("OL" beyond it), and blind to DC if it is a Rogowski coil. Give an emulator an
InstrumentModel and it answers from the model instead of its formula.
"""
import math
import random
import threading
from typing import Any, Dict, Optional

SIGNALS = ("dc", "ac")
# Real meters keep reading a little past full scale before showing "OL"
OVER_RANGE_HEADROOM = 0.05


class ReferenceSource:
    """
    The true current applied to the instruments under test, like a calibrated current source.
    `signal` is "dc" or "ac" (then `current_a` is the RMS value). `relative_uncertainty` is the
    source's own expanded (k = 2) uncertainty as a fraction of the output.
    """

    def __init__(self, current_a: float = 0.0, signal: str = "dc", relative_uncertainty: float = 1e-4):
        if signal not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}, got {signal!r}")
        if relative_uncertainty < 0:
            raise ValueError("relative_uncertainty must not be negative")
        self._lock = threading.Lock()
        self._current_a = float(current_a)
        self.signal = signal
        self.relative_uncertainty = relative_uncertainty

    @property
    def current_a(self) -> float:
        with self._lock:
            return self._current_a

    def set(self, current_a: float, signal: Optional[str] = None) -> None:
        if signal is not None and signal not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}, got {signal!r}")
        with self._lock:
            self._current_a = float(current_a)
            if signal is not None:
                self.signal = signal

    def standard_uncertainty_a(self) -> float:
        """The source's standard uncertainty (k = 1) at its present output."""
        return self.relative_uncertainty * abs(self.current_a) / 2


class InstrumentModel:
    """
    How one real meter turns the reference current into a reading.

    reading = true × (1 + gain_error) + offset_a + noise_floor_a·N(0,1) + relative_noise·|true|·N(0,1),
    quantized to `resolution_a`; "OL" when |reading| exceeds the range (plus 5 % headroom).
    A meter with measures_dc=False (a Rogowski coil, which senses dI/dt) reads only its noise on DC.
    `accuracy_percent_of_reading` and `accuracy_counts` are the datasheet spec the meter is tested
    against: ±(x % of reading + y counts), one count being one step of `resolution_a`.
    `burden_ohm` is the meter's series resistance: the voltage it drops is taken from the circuit.
    """

    def __init__(self, source: ReferenceSource, range_a: float, resolution_a: float, gain_error: float = 0.0,
                 offset_a: float = 0.0, noise_floor_a: float = 0.0, relative_noise: float = 0.0,
                 measures_dc: bool = True, burden_ohm: float = 0.0,
                 accuracy_percent_of_reading: Optional[float] = None, accuracy_counts: Optional[float] = None,
                 seed: Any = None):
        if range_a <= 0 or resolution_a <= 0 or resolution_a >= range_a:
            raise ValueError("need 0 < resolution_a < range_a")
        for name, value in (("noise_floor_a", noise_floor_a), ("relative_noise", relative_noise),
                            ("burden_ohm", burden_ohm)):
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")
        if (accuracy_percent_of_reading is None) != (accuracy_counts is None):
            raise ValueError("give both accuracy_percent_of_reading and accuracy_counts, or neither")
        self.source = source
        self.range_a = range_a
        self.resolution_a = resolution_a
        self.gain_error = gain_error
        self.offset_a = offset_a
        self.noise_floor_a = noise_floor_a
        self.relative_noise = relative_noise
        self.measures_dc = measures_dc
        self.burden_ohm = burden_ohm
        self.accuracy_percent_of_reading = accuracy_percent_of_reading
        self.accuracy_counts = accuracy_counts
        # Digits shown: enough for one step of the resolution (0.001 A -> 3 decimals)
        self.decimals = max(0, -math.floor(math.log10(resolution_a) + 1e-9))
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Dict[str, Any], source: ReferenceSource, seed: Any = None) -> "InstrumentModel":
        """Build from one entry of the config's accuracy_verification.instruments section."""
        allowed = {"range_a", "resolution_a", "gain_error", "offset_a", "noise_floor_a", "relative_noise",
                   "measures_dc", "burden_ohm", "accuracy_percent_of_reading", "accuracy_counts", "test_signal"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"Unknown instrument settings {sorted(unknown)}. Allowed: {sorted(allowed)}")
        settings = {k: v for k, v in config.items() if k != "test_signal"}
        return cls(source, seed=seed, **settings)

    def sensed_current_a(self) -> float:
        """The current the sensor actually responds to (a Rogowski coil sees nothing on DC)."""
        if self.source.signal == "dc" and not self.measures_dc:
            return 0.0
        return self.source.current_a

    def read_reply(self) -> str:
        """One reading as the meter would send it: fixed decimals, or "OL" when over range."""
        true = self.sensed_current_a()
        with self._lock:
            value = (true * (1 + self.gain_error) + self.offset_a
                     + self._rng.gauss(0, self.noise_floor_a) + self._rng.gauss(0, self.relative_noise * abs(true)))
        if abs(value) > self.range_a * (1 + OVER_RANGE_HEADROOM):
            return "OL"
        quantized = round(value / self.resolution_a) * self.resolution_a
        text = f"{quantized:.{self.decimals}f}"
        return "0" + text[2:] if text.startswith("-0") and float(text) == 0 else text     # no "-0.000"

    def has_spec(self) -> bool:
        return self.accuracy_percent_of_reading is not None

    def tolerance_a(self, reading_a: float) -> float:
        """The datasheet tolerance at a reading: x % of reading + y counts."""
        percent, counts = self.accuracy_percent_of_reading, self.accuracy_counts
        if percent is None or counts is None:
            raise ValueError("this instrument has no accuracy spec")
        return percent / 100 * abs(reading_a) + counts * self.resolution_a

    def resolution_uncertainty_a(self) -> float:
        """Type B standard uncertainty from quantization: a rectangular distribution one count wide."""
        return self.resolution_a / math.sqrt(12)
