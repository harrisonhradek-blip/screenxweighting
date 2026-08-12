"""Command-line interface for building and viewing market-rank snapshots.

This module owns user interaction and file caching. The scoring formula itself
lives in :mod:`market_rank.metrics`, so additions to the model generally do not
need to change the CLI.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import time
from typing import Any
import math

from .config import (
    DEFAULT_OPEN_REFRESH_TIME,
    FUNDAMENTALS_FILE,
    FUNDAMENTALS_TTL_DAYS,
    SNAPSHOT_FILE,
    UNIVERSE_FILE,
)
from .metrics import METRICS, display_metrics, normalize_record, score_records
from .market_cap import largest_us_equities
from .storage import load, now_iso, save
from .universe import download_us_symbols
from .weights import load_sector_weights


def _fresh(entry: dict[str, Any]) -> bool:
    """Return whether a cached fundamentals record is still reusable."""
    try:
        expiry = datetime.now(timezone.utc) - timedelta(days=FUNDAMENTALS_TTL_DAYS)
        return datetime.fromisoformat(entry["fetched_at"]) > expiry
    except (KeyError, TypeError, ValueError):
        return False


def update(args: argparse.Namespace) -> None:
    """Fetch or reuse company data, score it, and persist the top 100 snapshot."""
    import yfinance as yf

    stored_universe = load(UNIVERSE_FILE, {})
    if args.symbols:
        symbols = [s.upper() for s in args.symbols.split(",")]
    elif args.market_cap:
        print(
            f"Getting the {args.market_cap:,} largest US common equities "
            "by market capitalization..."
        )
        symbols = largest_us_equities(args.market_cap)
    elif stored_universe.get("symbols") and not args.refresh_universe:
        symbols = stored_universe["symbols"]
    else:
        print("Downloading US-listed symbol directory...")
        symbols = download_us_symbols()
        save(UNIVERSE_FILE, {"fetched_at": now_iso(), "symbols": symbols})
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    # Fundamentals change slowly; cache them to avoid thousands of duplicate
    # upstream requests on every daily update.
    cache: dict[str, dict[str, Any]] = load(FUNDAMENTALS_FILE, {})
    records: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols, start=1):
        cached = cache.get(symbol)
        if cached and _fresh(cached):
            records.append(cached["record"])
            continue
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            if not info or not info.get("quoteType", "EQUITY") == "EQUITY":
                continue
            info["symbol"] = symbol
            history = ticker.history(period="5d", auto_adjust=False, raise_errors=False)
            record = normalize_record(info, history)
            cache[symbol] = {"fetched_at": now_iso(), "record": record}
            records.append(record)
        except Exception as exc:  # Individual bad tickers must not stop a market run.
            print(f"[{index}/{len(symbols)}] {symbol}: {exc}")
        if index % 25 == 0:
            save(FUNDAMENTALS_FILE, cache)
            print(f"Processed {index}/{len(symbols)} symbols")
    ranked = score_records(records, load_sector_weights(args.weights_file))
    eligible = [
        record for record in ranked if record.get("coverage", 0) >= args.min_coverage
    ]
    snapshot = {
        "generated_at": now_iso(),
        "universe_size": len(symbols),
        "ranked_count": len(ranked),
        "eligible_count": len(eligible),
        "min_coverage": args.min_coverage,
        "universe": (
            f"largest {args.market_cap:,} US equities"
            if args.market_cap
            else "all US listed equities"
        ),
        # Store only the user-facing leaderboard; raw fundamentals remain cached.
        "records": eligible[:100],
    }
    save(FUNDAMENTALS_FILE, cache)
    save(SNAPSHOT_FILE, snapshot)
    print(
        f"Saved {len(snapshot['records'])} eligible companies "
        f"(coverage >= {args.min_coverage}) to {SNAPSHOT_FILE}"
    )


def _fmt(value: Any, percent: bool = False) -> str:
    """Format values consistently for terminal tables, including missing values."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.1%}" if percent else f"{value:.2f}"


def top(args: argparse.Namespace) -> None:
    """Print the saved leaderboard without downloading fresh market data."""
    snapshot = load(SNAPSHOT_FILE, {})
    records = snapshot.get("records", [])
    if not records:
        raise SystemExit("No snapshot yet. Run: python -m market_rank update")
    records = [row for row in records if row.get("coverage", 0) >= args.min_coverage]
    print(
        f"Snapshot: {snapshot.get('generated_at')} | "
        f"{snapshot.get('universe', 'US equities')} | "
        f"ranked {snapshot.get('ranked_count')} of {snapshot.get('universe_size')} "
        f"symbols | coverage >= {args.min_coverage}"
    )
    print(
        f"{'#':>3}  {'Ticker':<7} {'Company':<28} {'Sector':<20} "
        f"{'Score':>7} {'Coverage':>8}"
    )
    for rank, row in enumerate(records[:args.limit], start=1):
        print(
            f"{rank:>3}  {row['symbol']:<7} {row['name'][:28]:<28} "
            f"{row['sector'][:20]:<20} {_fmt(row.get('composite_score')):>7} "
            f"{row.get('coverage', 0):>8}"
        )


def show(args: argparse.Namespace) -> None:
    """Print the raw and sector-relative scores for one saved leaderboard entry."""
    records = load(SNAPSHOT_FILE, {}).get("records", [])
    row = next((r for r in records if r["symbol"] == args.symbol.upper()), None)
    if not row:
        raise SystemExit(
            f"{args.symbol.upper()} is not in the current top 100. "
            "Run update or use a ranked symbol."
        )
    print(f"{row['name']} ({row['symbol']}) — {row['sector']} / {row['industry']}")
    print(
        f"Composite score: {_fmt(row.get('composite_score'))}  |  "
        f"price: {_fmt(row.get('price'))}  |  "
        f"analyst target: {_fmt(row.get('analyst_target'))}"
    )
    print("\nMetric                         Raw value       Relative score")
    for label, raw, score in display_metrics(row):
        pct = label in {"Revenue growth", "EPS growth", "Forward ROE"}
        print(f"{label:<29} {_fmt(raw, pct):>11}       {_fmt(score):>8}")
    print("\nCategory scores (sector-weighted composite)")
    for category in ("future", "financial_health", "valuation"):
        print(f"{category.replace('_', ' ').title():<29} {_fmt(row.get(f'category_{category}')):>11}")
    print(
        "DCF scenarios (per share): bear {}, base {}, bull {}; base/current = {}".format(
            _fmt(row.get("dcf_bear")),
            _fmt(row.get("dcf_base")),
            _fmt(row.get("dcf_bull")),
            _fmt(row.get("dcf_upside")),
        )
    )
    print(f"Analyst target/current: {_fmt(row.get('analyst_upside'))}")
    print("Scores > 1 are better than the sector median. For valuation/debt metrics, lower raw values score higher.")


def run(args: argparse.Namespace) -> None:
    """Run a lightweight scheduler that refreshes once after each NYSE open."""
    import pandas as pd
    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    last_date = None
    print(f"Watching for NYSE market days; refresh time is {args.at} America/New_York. Ctrl-C to stop.")
    while True:
        now = pd.Timestamp.now(tz="America/New_York")
        day = now.strftime("%Y-%m-%d")
        schedule = nyse.schedule(start_date=day, end_date=day)
        if not schedule.empty and now.strftime("%H:%M") >= args.at and last_date != day:
            update(args)
            last_date = day
        time.sleep(30)


def main() -> None:
    """Parse CLI commands and dispatch to their implementation functions."""
    parser = argparse.ArgumentParser(
        prog="market-rank",
        description="Rank US equities using sector-relative fundamentals.",
    )
    sub = parser.add_subparsers(required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--symbols",
        help="Comma-separated tickers (useful for a quick or focused run).",
    )
    common.add_argument("--max-symbols", type=int, help="Limit universe size (testing only).")
    common.add_argument(
        "--market-cap",
        type=int,
        choices=(100, 1000),
        help="Rank only the largest 100 or 1,000 US common equities by live market cap.",
    )
    common.add_argument("--refresh-universe", action="store_true", help="Redownload listed symbols.")
    common.add_argument(
        "--min-coverage",
        type=int,
        default=0,
        choices=range(0, 13),
        metavar="0-12",
        help="Require this many valid metrics before saving/displaying (recommended: 7).",
    )
    common.add_argument(
        "--weights-file",
        help="Optional JSON file with default and sector category weights.",
    )

    update_parser = sub.add_parser("update", parents=[common])
    update_parser.set_defaults(func=update)

    top_parser = sub.add_parser("top")
    top_parser.add_argument("--limit", type=int, default=100)
    top_parser.add_argument(
        "--min-coverage", type=int, default=0, choices=range(0, 13), metavar="0-12"
    )
    top_parser.set_defaults(func=top)

    show_parser = sub.add_parser("show")
    show_parser.add_argument("symbol")
    show_parser.set_defaults(func=show)

    run_parser = sub.add_parser("run", parents=[common])
    run_parser.add_argument(
        "--at", default=DEFAULT_OPEN_REFRESH_TIME, help="HH:MM ET, default 09:40"
    )
    run_parser.set_defaults(func=run)
    args = parser.parse_args()
    args.func(args)
