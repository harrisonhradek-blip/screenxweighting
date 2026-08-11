"""Configurable category weights for sector-aware composite scores."""
from __future__ import annotations

import json
from pathlib import Path


# Sector-specific allocations should be agreed by users or selected from a
# validated backtest. Equal category weights are the safe default.
DEFAULT_WEIGHTS = {"future": 1 / 3, "financial_health": 1 / 3, "valuation": 1 / 3}


def load_sector_weights(path: str | None) -> dict[str, dict[str, float]]:
    """Load a JSON object keyed by sector name, or use neutral defaults."""
    profiles: dict[str, dict[str, float]] = {"default": DEFAULT_WEIGHTS}
    if not path:
        return profiles
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("Weights file must be a JSON object keyed by sector name.")
    required = set(DEFAULT_WEIGHTS)
    for sector, values in data.items():
        if not isinstance(values, dict) or set(values) != required:
            raise ValueError(f"{sector}: supply exactly {sorted(required)}.")
        parsed = {key: float(values[key]) for key in required}
        if any(value <= 0 for value in parsed.values()):
            raise ValueError(f"{sector}: all category weights must be positive.")
        total = sum(parsed.values())
        profiles[str(sector)] = {key: value / total for key, value in parsed.items()}
    return profiles
