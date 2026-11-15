# Bagel

A NautilusTrader lab for pulling historical market data from **Interactive
Brokers** and backtesting strategies offline. Classified schmear research.

- Python **3.14** (managed by `uv`), NautilusTrader **2.0.0rc9**
- Data source: **IB Gateway** (paper) over the TWS API, port `4002`
- Base granularities: **1-minute** (intraday) and **daily**

## Setup

```bash
uv sync                 # install locked dependencies into .venv
```

IB Gateway must be running and logged in (paper), with the API enabled
(Configure → Settings → API: *Enable ActiveX and Socket Clients*, trusted IP
`127.0.0.1`, socket port `4002`). Connection settings default to those values
and can be overridden via `.env` (see `.env.example`).

## Data model

Two series are **downloaded** directly from IB and stored in the catalog:

| series         | default history | why download it                                   |
| -------------- | --------------- | ------------------------------------------------- |
| 1-minute       | 2 years         | the intraday **base**; deepest practical from IB  |
| daily          | 15 years        | long history, trivial to pull                     |

Everything **below daily** (5-/15-/60-minute, etc.) is **reconstructed from the
1-minute base** on the fly via Nautilus composite bars — never downloaded
separately. See `multi_tf` and `universe.composite_bar_type`.

```bash
uv run bagel-download                       # 1-min, 2y, all symbols
uv run bagel-download --timeframe 1day      # daily, 15y
uv run bagel-download --timeframe all       # both
uv run bagel-download --symbols SPY UPRO    # subset
uv run bagel-download --timeframe 1day --force   # re-pull
```

Universe: `SPY QQQ IWM DIA TLT GLD UPRO TQQQ` — all on IB's `ARCA` venue with
consolidated trades. Already-present series are skipped (resumable); a symbol
with shallow deep-history (e.g. TLT daily only back to 2016 on ARCA) auto-falls
back to a shorter window instead of failing the batch.

Sizes: 1-min ≈ 194k bars/symbol (~26 MB for all 8); daily ≈ 3.8k bars/symbol.
Small enough to keep local; `CATALOG_PATH` can point elsewhere (e.g. an external
drive) for larger vendor datasets.

## Backtest

```bash
uv run bagel-backtest ema_cross UPRO --fast 20 --slow 50
uv run bagel-backtest multi_tf  TQQQ --minutes 15
uv run bagel-backtest sma_trend UPRO --period 200
uv run bagel-backtest rsi_reversion SPY --period 14 --lower 30 --upper 70
uv run bagel-backtest donchian_breakout TQQQ --period 20
```

The runner automatically feeds the engine the right stored series based on the
strategy (1-minute for intraday/composite strategies, daily for the daily ones).

Strategies (`src/bagel/strategies/`):

| name                | style          | timeframe | idea                                             |
| ------------------- | -------------- | --------- | ------------------------------------------------ |
| `buy_and_hold`      | benchmark      | 1-min     | buy once, hold (reported mark-to-market)         |
| `ema_cross`         | trend          | 1-min     | fast/slow EMA crossover                          |
| `multi_tf`          | trend          | composite | EMA cross on a timeframe rebuilt from 1-min base |
| `sma_trend`         | trend filter   | daily     | hold while price > SMA (classic 200-day)         |
| `rsi_reversion`     | mean-reversion | daily     | buy oversold RSI, exit when it recovers          |
| `donchian_breakout` | breakout       | daily     | buy N-day highs, exit on N-day lows              |

## Layout

```
src/bagel/
  config.py            # settings from env/.env (IB host/port, catalog path, capital)
  universe.py          # 8 ETFs + base / daily / composite bar-type helpers
  ingest/
    catalog.py         # open the ParquetDataCatalog
    ib_history.py      # pull 1-min & daily bars from IB Gateway   (bagel-download)
  strategies/          # base.py + six sample strategies
  backtest/
    run.py             # catalog -> BacktestEngine -> strategy -> summary (bagel-backtest)
data/
  catalog/             # ParquetDataCatalog (gitignored, regenerable)
  raw/                 # raw CSVs (e.g. FirstRate imports) before conversion
```

## Code quality

`pre-commit` runs **ruff** (lint with autofix) and **ruff-format**:

```bash
pre-commit run --all-files
```

The ruff ruleset is pinned in `pyproject.toml` (`[tool.ruff.lint]`) for
reproducibility, since ruff's defaults drift between releases.

## Notes & caveats

- Sample strategies trade a **fixed 100 shares**, a small slice of the $1M book,
  so reported returns are small — position sizing is a deliberate next knob.
- Backtests use a **zero-fee, margin, USD** simulated ARCA venue. Add a realistic
  fee model before trusting absolute PnL.
- Live data/exec clients are **not wired yet**; this repo covers historical
  ingest + offline backtesting.

### FirstRate / deeper data (future)

Deeper intraday history (e.g. FirstRate `ES_full_1min_continuous_adjusted`, a
1-minute continuous back-adjusted E-mini S&P 500 series) drops into `data/raw/`.
Futures need contract-aware handling (point multiplier, roll adjustments) and a
small loader that converts the CSV into the same ParquetDataCatalog — a separate
venue/instrument from the ETF cash equities above.
