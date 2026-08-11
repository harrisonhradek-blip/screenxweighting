# Market Rank CLI

A small Python CLI that refreshes a US-listed equity ranking shortly after the US market opens, stores the result locally, and displays the top 100 companies with sector-relative valuation, financial-health, and growth metrics.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Optional: install the `market-rank` command as well.
pip install -e .
```

## Use

```bash
# Download the listed US equity universe and calculate a current snapshot.
market-rank update

# Faster focused universes: the largest US companies by live market cap.
market-rank update --market-cap 100
market-rank update --market-cap 1000

# Exclude incomplete companies before saving the ranked top 100.
market-rank update --market-cap 1000 --min-coverage 7
market-rank top --min-coverage 7

# Print the current top 100 (or a shorter list).
market-rank top --limit 25

# Inspect one company and its sector-relative scores.
market-rank show MSFT

# Keep the process alive; refreshes once on each NYSE trading day at 09:40 ET.
market-rank run
```

`update` is intentionally explicit: the entire US-listed universe is large and the upstream quote/fundamental endpoints can be slow or rate-limited. The CLI caches company fundamentals for 7 days and uses the most recent daily history for price-based data. On a typical connection, the first full run may take a while.

For an unattended process, run `market-rank run` under your usual service manager (launchd/systemd) or invoke `market-rank update` from a scheduler at 09:40 America/New_York on trading days.

## How scores work

For every metric, the raw comparison is against the **median valid value in the company’s sector**. A score of `1.00` means the sector median. Values above one are better:

- Growth, ROE, asset turnover, cash flow and analyst upside use `company / sector median`.
- PE, forward PE, EV/EBITDA, debt-to-equity and historic PE use `sector median / company` because lower is better.

Negative or zero values are shown as unavailable for ratio-based comparisons where they would be misleading. The composite rank is a winsorized average of available relative scores, plus DCF and analyst-upside contributions; it is a screening signal, not investment advice.

## Sector category weights

The composite first averages valid scores inside Future, Financial health, and Valuation categories, then weights those three category scores. This prevents a company with more reported valuation fields from being over-represented. The default is equal category weighting. Supply a reviewed profile when your group has agreed one:

```bash
market-rank update --market-cap 1000 --min-coverage 7 --weights-file sector-weights.example.json
```

The included example has the Technology split: Future 40%, Financial health 30%, Valuation 30%.

## Data notes

- The stock universe comes from Nasdaq Trader’s public `nasdaqlisted.txt` and `otherlisted.txt` directories, filtered to common equity-like listings. ETFs, funds, test issues and non-US symbol formats are excluded.
- Fundamentals, analyst targets, earnings/revenue growth and daily prices are fetched through `yfinance`/Yahoo Finance. Coverage varies by company; missing metrics do not count against a company’s composite.
- DCF scenarios use a deliberately transparent five-year FCFE-style projection based on reported free cash flow, revenue/EPS growth forecasts, beta-derived discounting and a Monte Carlo terminal-growth/discount-rate simulation. They are estimates, not analyst models.
