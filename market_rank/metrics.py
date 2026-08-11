"""Metric extraction, sector-relative scoring, and a lightweight DCF model."""
from __future__ import annotations

from typing import Any
import math
import numpy as np
import pandas as pd


METRICS: dict[str, tuple[str, bool, str]] = {
    "pe": ("trailingPE", False, "PE"),
    "forward_pe": ("forwardPE", False, "Forward PE"),
    "historic_pe": ("historic_pe", False, "Historic PE"),
    "debt_to_equity": ("debtToEquity", False, "Debt/equity"),
    "asset_turnover": ("asset_turnover", True, "Asset turnover"),
    "free_cash_flow": ("freeCashflow", True, "Free cash flow"),
    "revenue_growth": ("revenueGrowth", True, "Revenue growth"),
    "earnings_growth": ("earningsGrowth", True, "EPS growth"),
    "return_on_equity": ("returnOnEquity", True, "Forward ROE"),
    "forward_ev_ebitda": ("forwardEVToEBITDA", False, "Forward EV/EBITDA"),
}

# Categories receive their sector weight once, irrespective of how many raw
# fields Yahoo happens to report for that company.
CATEGORY_METRICS = {
    "future": ("revenue_growth", "earnings_growth", "return_on_equity"),
    "financial_health": ("debt_to_equity", "asset_turnover", "free_cash_flow"),
    "valuation": ("pe", "forward_pe", "historic_pe", "forward_ev_ebitda", "dcf_upside", "analyst_upside"),
}


def number(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def annualized_pe(history: pd.DataFrame, trailing_eps: float | None) -> float | None:
    if history.empty or not trailing_eps or trailing_eps <= 0:
        return None
    close = number(history["Close"].iloc[-1])
    return close / trailing_eps if close and close > 0 else None


def asset_turnover(info: dict[str, Any]) -> float | None:
    revenue, assets = number(info.get("totalRevenue")), number(info.get("totalAssets"))
    return revenue / assets if revenue and assets and assets > 0 else None


def _discount_rate(info: dict[str, Any]) -> float:
    beta = number(info.get("beta")) or 1.0
    return float(np.clip(0.035 + beta * 0.055, 0.07, 0.18))


def dcf_scenarios(info: dict[str, Any], runs: int = 2000) -> dict[str, float | None]:
    """Return per-share bear/base/bull DCF estimates using Monte Carlo paths."""
    fcf, shares, price = (number(info.get(k)) for k in ("freeCashflow", "sharesOutstanding", "currentPrice"))
    if not fcf or fcf <= 0 or not shares or shares <= 0 or not price or price <= 0:
        return {"dcf_bear": None, "dcf_base": None, "dcf_bull": None, "dcf_upside": None}
    forecast = number(info.get("earningsGrowth")) or number(info.get("revenueGrowth")) or 0.05
    forecast = float(np.clip(forecast, -0.10, 0.30))
    rng = np.random.default_rng(abs(hash(str(info.get("symbol", "")))) % (2**32))
    growth = rng.normal(forecast, 0.08, (runs, 5)).clip(-0.30, 0.40)
    discount = rng.normal(_discount_rate(info), 0.015, runs).clip(0.06, 0.22)
    terminal_growth = rng.normal(0.025, 0.008, runs).clip(0.005, 0.04)
    cashflows = fcf * np.cumprod(1 + growth, axis=1)
    years = np.arange(1, 6)
    pv = (cashflows / (1 + discount[:, None]) ** years).sum(axis=1)
    terminal = cashflows[:, -1] * (1 + terminal_growth) / (discount - terminal_growth)
    values = (pv + terminal / (1 + discount) ** 5) / shares
    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        return {"dcf_bear": None, "dcf_base": None, "dcf_bull": None, "dcf_upside": None}
    bear, base, bull = np.percentile(values, [20, 50, 80])
    return {"dcf_bear": float(bear), "dcf_base": float(base), "dcf_bull": float(bull), "dcf_upside": float(base / price)}


def normalize_record(info: dict[str, Any], history: pd.DataFrame) -> dict[str, Any]:
    row = {key: number(info.get(source)) for key, (source, _, _) in METRICS.items()}
    row["historic_pe"] = annualized_pe(history, number(info.get("trailingEps")))
    row["asset_turnover"] = asset_turnover(info)
    target, price = number(info.get("targetMeanPrice")), number(info.get("currentPrice"))
    row.update(dcf_scenarios(info))
    row["analyst_upside"] = target / price if target and price and target > 0 and price > 0 else None
    row.update({
        "symbol": info.get("symbol"), "name": info.get("longName") or info.get("shortName") or info.get("symbol"),
        "sector": info.get("sector") or "Unknown", "industry": info.get("industry") or "Unknown",
        "price": price, "analyst_target": target,
    })
    return row


def score_records(records: list[dict[str, Any]], sector_weights: dict[str, dict[str, float]] | None = None) -> list[dict[str, Any]]:
    frame = pd.DataFrame(records)
    if frame.empty:
        return records
    for metric, (_, higher_is_better, _) in METRICS.items():
        values = pd.to_numeric(frame[metric], errors="coerce")
        valid = values.where(values > 0)  # Relative ratios are misleading for <= 0.
        median = valid.groupby(frame["sector"]).transform("median")
        score = values / median if higher_is_better else median / values
        frame[f"score_{metric}"] = score.where((values > 0) & (median > 0))
    # DCF and analyst targets are already price-relative: >1 means upside.
    for metric in ("dcf_upside", "analyst_upside"):
        frame[f"score_{metric}"] = pd.to_numeric(frame[metric], errors="coerce").where(lambda s: s > 0)
    score_cols = [c for c in frame if c.startswith("score_")]
    clipped = frame[score_cols].clip(lower=0.25, upper=4.0)
    frame["coverage"] = clipped.notna().sum(axis=1)
    sector_weights = sector_weights or {"default": {"future": 1 / 3, "financial_health": 1 / 3, "valuation": 1 / 3}}
    for category, metrics in CATEGORY_METRICS.items():
        frame[f"category_{category}"] = clipped[[f"score_{metric}" for metric in metrics]].mean(axis=1, skipna=True)

    def weighted_composite(row: pd.Series) -> float | None:
        weights = sector_weights.get(row["sector"], sector_weights["default"])
        available = [(row[f"category_{category}"], weights[category]) for category in CATEGORY_METRICS if pd.notna(row[f"category_{category}"])]
        if not available:
            return None
        return sum(score * weight for score, weight in available) / sum(weight for _, weight in available)

    frame["composite_score"] = frame.apply(weighted_composite, axis=1)
    return frame.sort_values(["composite_score", "coverage"], ascending=False).to_dict("records")


def display_metrics(row: dict[str, Any]) -> list[tuple[str, float | None, float | None]]:
    return [(label, number(row.get(metric)), number(row.get(f"score_{metric}"))) for metric, (_, _, label) in METRICS.items()]
