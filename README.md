# supertree

Personal family portfolio tracker and plain-language investing teacher. Layered dashboard: a simple front page ("Phua JR") answering *is my money growing and why*, and depth tabs with total-return, drawdown and trend context. The visual system is a supertree that grows with the portfolio — the frame is money put in, the living growth is what market and dividends added.

**Status:** Session 1 — engine, ledger, pipeline, guard. Page shell only; full front page and depth tabs follow.

## Architecture

```
supertree/
├── template.html          # source page (<200KB), fetch fallback for local dev
├── data/
│   ├── transactions.json  # SOURCE OF TRUTH — hand-maintained ledger
│   ├── prices.json        # daily OHLC + adjclose, written by scripts/fetch.py
│   ├── dividends.json     # ex-date + DPS, written by scripts/fetch.py
│   ├── portfolio.json     # engine output — position, decomposition, risk stats
│   ├── education.json     # engine output — long-history context, drawdown episodes
│   └── status.json        # refresh status + guard verdict, always written
├── scripts/
│   ├── fetch.py           # Yahoo pull with replacement guard (keep-last-good)
│   ├── guard.py           # validation: price frame, replacement, ledger
│   ├── engine.py          # valuation, decomposition, XIRR, drawdowns, milestones
│   ├── tests.py           # zero-dependency test suite (run: python scripts/tests.py)
│   └── pipeline.py        # engine outputs + inject into template → docs/index.html
└── docs/                  # GitHub Pages output — never edit by hand
```

- Refresh: `python scripts/fetch.py` then `python scripts/pipeline.py` (or `python scripts/pipeline.py --refresh` for both). GitHub Actions runs this on weekday evenings (21:00 SGT, after SGX close).
- Local dev: `npx serve .` and open `template.html` — it fetch-falls-back to `data/`.
- Tests: `python scripts/tests.py` — the Actions refresh runs them first and stops on failure.

## Ledger conventions (`data/transactions.json`)

One row per event, dates are ISO `YYYY-MM-DD` and must be SGX trading dates:

```json
{"date": "2025-06-30", "type": "BUY",  "ticker": "ES3.SI", "units": 400, "price": null, "fees": null, "provisional": true, "note": "..."}
{"date": "2025-08-01", "type": "DIV",  "ticker": "ES3.SI", "units": 400, "dps": 0.05,  "amount": null, "provisional": true, "note": "..."}
```

- `BUY`/`SELL`: `price` per unit; `fees` in S$. A `null` price on a `provisional` row is resolved to that day's close by the engine and flagged on the page.
- `DIV`: cash received, not reinvested. `amount` overrides `units × dps` when the actual credited cash is known.
- `provisional: true` marks estimated rows; every figure derived from them renders with a visible provisional marker until the flag is cleared.
- Guard rules: dates must exist in the price index; non-provisional `BUY`/`SELL` prices must lie within that day's low–high range; units must stay non-negative cumulatively.

## Guard layer

No unattended refresh without a guard. `fetch.py` replaces `prices.json` only if the new series is non-empty, at least as long as the existing one, ends no earlier, has strictly increasing dates, positive closes, no implausible daily move, and matches the existing series on overlapping recent dates (raw close, tight tolerance — catches silent restatements and ticker identity switches). On failure the last good data is kept and `status.json` records the reason; the page shows a stale badge whenever the data is older than 7 days, so a dead refresh is visible rather than silent.

## Data sources

- Prices and dividends: Yahoo Finance via `yfinance` (ES3.SI — SPDR Straits Times Index ETF, SGD). Ticker verified against the issuer product page at build time.
- Valuation uses raw close only. Adjusted close is used solely for long-history context charts and is labelled as such — never in the valuation path, so dividends are not double-counted.

*Last updated: 2026-07-18.*
