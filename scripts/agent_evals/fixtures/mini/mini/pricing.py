"""Price helpers."""

RANGE_FACTOR = 1.25


def _range(cost: float) -> tuple[float, float]:
    """Return the low and high bound around a cost."""
    return cost, cost * RANGE_FACTOR


def low_bound(cost: float) -> float:
    """Return the low bound of a cost."""
    low, _high = _range(cost)
    return low


def high_bound(cost: float) -> float:
    """Return the high bound of a cost."""
    _low, high = _range(cost)
    return high
