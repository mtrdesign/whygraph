from __future__ import annotations

from dataclasses import dataclass

from whygraph.cochange.types import CoChangeReport, VolatilityReport
from whygraph.neighbors import RationaleNeighbors


@dataclass(frozen=True)
class RationaleContext:
    """Bundle of structural + historical signals fed into the rationale prompt.

    Lives in its own module (rather than `neighbors.py` or `cochange/`)
    because it composes types from multiple subsystems. Adding a new signal
    is a field addition here + a fingerprint contribution at the bundle-hash
    site in `mcp_server.py` — no signature changes ripple through the
    rationale path.
    """

    neighbors: RationaleNeighbors
    cochange: CoChangeReport
    volatility: VolatilityReport
