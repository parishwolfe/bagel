# Bagel

![OCaml](OCaml.png)

A NautilusTrader lab for pulling historical market data from **Interactive
Brokers** and backtesting strategies offline. Classified schmear research.

- Python **3.14** (managed by `uv`), NautilusTrader **2.0.0rc9**
- Data sources: **IB Gateway** (paper, TWS API port `4002`) for ETFs, and
  **FirstRate** vendor CSVs for ES futures
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

### ES futures (FirstRate)

`bagel-firstrate` ingests the **E-mini S&P 500 (ES)** continuous futures series
from FirstRate vendor zips into the same catalog:

```bash
uv run bagel-firstrate                     # daily, continuous back-adjusted (2008–2023)
uv run bagel-firstrate --timeframe 1min    # 1-minute (~5.5M bars)
uv run bagel-firstrate --timeframe all     # both
```

- **Source** is read-only and configurable via `FIRSTRATE_SOURCE` (default the
  external drive); the zips are never modified.
- **Ingestion is ephemeral**: each run extracts the needed member to a temp
  directory, streams it straight into the catalog in chunks, and deletes the
  temp dir — nothing lands in `data/raw/`.
- **Continuous back-adjusted** means FirstRate already stitched the quarterly
  contracts (H/M/U/Z) into one roll-adjusted series, so there is nothing to
  combine. The per-contract `archive` + `update` zips would only need combining
  if you were stitching individual expiries yourself — which this series
  supersedes.
- **Futures economics** live in `src/bagel/futures.py`: CME Globex venue
  (`XCME`), `$50`/point multiplier, `0.25` tick, modelled as a Nautilus
  `FuturesContract`. Back-adjusted prices are synthetic in absolute terms (good
  for signals, not for literal price levels).

## Backtest

```bash
uv run bagel-backtest ema_cross UPRO --fast 20 --slow 50
uv run bagel-backtest multi_tf  TQQQ --minutes 15
uv run bagel-backtest sma_trend UPRO --period 200
uv run bagel-backtest rsi_reversion SPY --period 14 --lower 30 --upper 70
uv run bagel-backtest donchian_breakout TQQQ --period 20
uv run bagel-backtest es_trend ES --fast 20 --slow 50          # ES futures
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
| `es_trend`          | trend (futures)| daily     | long/short EMA cross on ES, sized in contracts   |

## Layout

```
src/bagel/
  config.py            # settings from env/.env (IB host/port, catalog path, capital)
  universe.py          # 8 ETFs + base / daily / composite bar-type helpers
  futures.py           # ES continuous futures instrument (CME, $50/pt) + bar types
  ingest/
    catalog.py         # open the ParquetDataCatalog
    ib_history.py      # pull 1-min & daily bars from IB Gateway   (bagel-download)
    firstrate.py       # ingest FirstRate ES futures, ephemerally  (bagel-firstrate)
  strategies/          # base.py + seven sample strategies
  backtest/
    run.py             # catalog -> BacktestEngine -> strategy -> summary (bagel-backtest)
data/
  catalog/             # ParquetDataCatalog (gitignored, regenerable)
  raw/                 # kept empty — FirstRate ingest is ephemeral (temp dir, no residue)
```

## Code quality

`pre-commit` runs **Gitleaks** (secret detection), **ruff** (lint with autofix),
and **ruff-format**:

```bash
pre-commit run --all-files
```

Gitleaks blocks commits when it detects secrets in staged changes and redacts
secret values in its output. Its hook scans staged changes even with `--all-files`;
it does not audit the repository's full contents or history. The first run builds
the pinned Gitleaks version using pre-commit's managed Go environment.

The ruff ruleset is pinned in `pyproject.toml` (`[tool.ruff.lint]`) for
reproducibility, since ruff's defaults drift between releases.

## Notes & caveats

- ETF sample strategies trade a **fixed 100 shares**, a small slice of the $1M
  book, so reported returns are small — position sizing is a deliberate next
  knob. `es_trend` trades **1 ES contract** (~$200k notional), so its PnL is a
  much larger slice.
- Backtests use a **zero-fee, margin, USD** simulated venue (ARCA for ETFs,
  XCME for ES). Add a realistic fee/commission model before trusting absolute
  PnL — futures commissions in particular are per-contract.
- ES prices are **back-adjusted continuous**: good for signals, not for literal
  historical price levels. FirstRate times are stored on a fixed clock (a
  constant offset that does not affect a single-instrument backtest).
- Live data/exec clients are **not wired yet**; this repo covers historical
  ingest + offline backtesting.
