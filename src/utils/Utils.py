import random
from typing import Optional


def generate_random_float(min_value: float, max_value: float, rng: Optional[random.Random] = None) -> float:
    """Generate a random float between min_value and max_value, from `rng` if given (else the global generator)."""
    return (rng or random).uniform(min_value, max_value)
