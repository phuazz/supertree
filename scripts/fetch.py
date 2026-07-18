"""Yahoo pull for ES3.SI with a keep-last-good replacement guard.

Writes data/prices.json + data/dividends.json only when the guard passes.
data/status.json is written on every run, pass or fail, so a dead refresh is
visible on the page rather than silent. Exit 0 on pass, 2 on guard failure.
"""
import json
import sys
from datetime import datetime, timezone  # Python datetime: months are 1-indexed
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
import guard

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TICKER = "ES3.SI"


def load_json(p):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write_json(p, obj):
    Path(p).write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")


def fetch_frame():
    t = yf.Ticker(TICKER)
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
        errs = guard.validate_price_rows(rows) + guard.validate_replacement(rows, old_rows)
    except Exception as e:
        rows, divs, meta = [], [], {}
        errs = [f"fetch failed: {e}"]
    if errs:
        status["guard"] = "fail: " + "; ".join(errs[:6])
        status["kept_rows"] = len(old_rows)
        status["last_price_date"] = old_rows[-1][0] if old_rows else None
        write_json(DATA / "status.json", status)
        print("GUARD FAIL — kept last-good data:\n  " + "\n  ".join(errs), file=sys.stderr)
        sys.exit(2)
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
    write_json(DATA / "status.json", status)
    print(f"OK {TICKER}: {len(rows)} rows {rows[0][0]} -> {rows[-1][0]}, {len(divs)} dividend events")
    print(f"meta: {meta}")


if __name__ == "__main__":
    main()
