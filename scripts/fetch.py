"""Yahoo pull for ES3.SI with a keep-last-good replacement guard.

Writes data/prices.json + data/dividends.json only when the guard passes.
data/status.json is written on every run, pass or fail, so a dead refresh is
visible on the page rather than silent. Exit 0 on pass, 2 on guard failure.

Watched funds (WATCH: shown on the page with 0 units, never valued) are
pulled after the holding into data/watch/<symbol>.json under the same guard
plus a currency identity check. A watch failure is recorded in
status["watch"] and keeps that fund's last-good file, but it NEVER changes
the exit code or the holding's data.
"""
import json
import sys
from datetime import datetime, timezone  # Python datetime: months are 1-indexed
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
import guard
import patches

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TICKER = "ES3.SI"
WATCH = {"GAB.SI": {"label": "Amova Singapore STI ETF, accumulating class", "currency": "SGD"}}
WATCH_DIR = DATA / "watch"


def load_json(p):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write_json(p, obj):
    Path(p).write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")


def fetch_frame(symbol=TICKER):
    t = yf.Ticker(symbol)
    hist = t.history(period="max", auto_adjust=False)
    if hist.empty:
        raise RuntimeError("Yahoo returned an empty frame")
    need = {"Open", "High", "Low", "Close", "Adj Close", "Dividends"}
    missing = need - set(hist.columns)
    if missing:
        raise RuntimeError(f"Yahoo frame missing columns: {sorted(missing)}")
    rows, divs = [], []
    for ts, rec in hist.iterrows():
        d = ts.strftime("%Y-%m-%d")  # index is exchange-local; date part = SGX session date
        vals = [rec["Open"], rec["High"], rec["Low"], rec["Close"], rec["Adj Close"]]
        if any(v is None or v != v for v in vals):  # NaN-safe without numpy import
            continue
        rows.append([d] + [round(float(v), 4) for v in vals])
        dv = rec["Dividends"]
        if dv == dv and dv is not None and float(dv) > 0:
            divs.append({"exdate": d, "dps": round(float(dv), 6)})
    meta = {}
    try:
        fi = t.fast_info
        meta["currency"] = getattr(fi, "currency", None)
        meta["exchange"] = getattr(fi, "exchange", None)
    except Exception as e:  # meta is best-effort; the guard does not depend on it
        meta["fast_info_error"] = str(e)
    try:
        meta["long_name"] = (t.info or {}).get("longName")
    except Exception as e:
        meta["info_error"] = str(e)
    return rows, divs, meta


def main():
    status = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": TICKER,
        "guard": None,
    }
    old = load_json(DATA / "prices.json")
    old_rows = old["rows"] if old else []
    try:
        rows, divs, meta = fetch_frame()
        pf = load_json(DATA / "price_patches.json")
        rows, patch_errs, patch_notes = patches.apply_patches(
            rows, pf["rows"] if pf else [], divs)
        for n in patch_notes:
            print(n)
        errs = (patch_errs + guard.validate_price_rows(rows)
                + guard.validate_replacement(rows, old_rows))
    except Exception as e:
        rows, divs, meta = [], [], {}
        errs = [f"fetch failed: {e}"]
    if errs:
        status["guard"] = "fail: " + "; ".join(errs[:6])
        status["kept_rows"] = len(old_rows)
        status["last_price_date"] = old_rows[-1][0] if old_rows else None
        print("GUARD FAIL — kept last-good data:\n  " + "\n  ".join(errs), file=sys.stderr)
    else:
        write_json(DATA / "prices.json",
                   {"ticker": TICKER,
                    "cols": ["date", "open", "high", "low", "close", "adjclose"],
                    "rows": rows})
        write_json(DATA / "dividends.json", {"ticker": TICKER, "dividends": divs})
        status["guard"] = "pass"
        status["rows"] = len(rows)
        status["last_price_date"] = rows[-1][0]
        status["dividend_events"] = len(divs)
        status["meta"] = meta
        print(f"OK {TICKER}: {len(rows)} rows {rows[0][0]} -> {rows[-1][0]}, {len(divs)} dividend events")
        print(f"meta: {meta}")
    status["watch"] = fetch_watch()
    write_json(DATA / "status.json", status)
    if errs:
        sys.exit(2)


def fetch_watch():
    """Pull each watched fund; keep its last-good file on any failure.
    Returns {symbol: status} for status.json. Never raises, never exits."""
    out = {}
    WATCH_DIR.mkdir(exist_ok=True)
    for sym, spec in WATCH.items():
        path = WATCH_DIR / f"{sym}.json"
        old = load_json(path)
        old_rows = old["rows"] if old else []
        try:
            rows, divs, meta = fetch_frame(sym)
            errs = guard.validate_watch_frame(rows, old_rows, meta, spec["currency"])
        except Exception as e:
            rows, divs, meta, errs = [], [], {}, [f"fetch failed: {e}"]
        if errs:
            out[sym] = {"guard": "fail: " + "; ".join(errs[:4]), "kept_rows": len(old_rows),
                        "last_price_date": old_rows[-1][0] if old_rows else None}
            print(f"WATCH {sym} GUARD FAIL — kept last-good:\n  " + "\n  ".join(errs), file=sys.stderr)
            continue
        write_json(path, {"ticker": sym, "label": spec["label"],
                          "cols": ["date", "open", "high", "low", "close", "adjclose"],
                          "rows": rows, "dividend_events": len(divs)})
        out[sym] = {"guard": "pass", "rows": len(rows), "last_price_date": rows[-1][0], "meta": meta}
        print(f"WATCH OK {sym}: {len(rows)} rows {rows[0][0]} -> {rows[-1][0]}  meta: {meta}")
    return out


if __name__ == "__main__":
    main()
