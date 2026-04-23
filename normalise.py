"""
Normalisation primitives.

Each source normalises its own output into a scalar roughly in [-1, +1]
using techniques appropriate to that signal's distribution. This module
holds the shared helpers; signal-specific normalisation lives in each
source module.

Key principle: we z-score against rolling history, not min-max against
arbitrary ceilings. This preserves the information content of unusual
movements relative to each signal's own baseline.
"""

import math
from typing import Sequence


def tanh_squash(x: float, scale: float = 1.0) -> float:
    """Map any real number to (-1, +1) via tanh."""
    return math.tanh(x / scale)


def z_score(value: float, history: Sequence[float]) -> float:
    """Z-score against a history series. Returns 0 if history too short."""
    if len(history) < 3:
        return 0.0
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance) or 1e-6
    return (value - mean) / std


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
