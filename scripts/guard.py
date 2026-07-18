"""Validation layer for supertree. Pure functions returning lists of error strings.

House rule: no unattended refresh without a guard. fetch.py and pipeline.py call
these and refuse to write (keeping last-good data) when any check fails.
"""

MAX_DAILY_MOVE = 0.20   # |1-day close move| above this fails the frame (broad-index ETF)
OVERLAP_DAYS = 30       # recent shared dates compared between old and new pulls
OVERLAP_TOL = 0.005     # raw close must match within 0.5% on shared dates
VALID_TYPES = {"BUY", "SELL", "DIV"}


def validate_price_rows(rows):
    """rows: list of [date, open, high, low, close, adjclose], date ISO YYYY-MM-DD."""
    errs = []
    if not rows:
        return ["price frame is empty"]
    prev_date, prev_close = None, None
    for i, r in enumerate(rows):
        if not isinstance(r, list) or len(r) != 6:
            errs.append(f"row {i}: expected 6 fields, got {r!r}")
            continue
        d, o, h, low, c, ac = r
        if prev_date is not None and d <= prev_date:
            errs.append(f"row {i} ({d}): dates not strictly increasing")
        for name, v in (("open", o), ("high", h), ("low", low), ("close", c), ("adjclose", ac)):
            if not isinstance(v, (int, float)) or v <= 0:
                errs.append(f"row {i} ({d}): {name} not positive ({v!r})")
        if isinstance(low, (int, float)) and isinstance(h, (int, float)) and low > h:
            errs.append(f"row {i} ({d}): low > high")
        if prev_close and isinstance(c, (int, float)) and c > 0:
            move = abs(c / prev_close - 1.0)
            if move > MAX_DAILY_MOVE:
                errs.append(f"row {i} ({d}): daily move {move:.1%} exceeds {MAX_DAILY_MOVE:.0%}")
        prev_date = d
        if isinstance(c, (int, float)) and c > 0:
            prev_close = c
    return errs


def validate_replacement(new_rows, old_rows):
    """Refuse to replace a longer or fresher history with a shorter or staler one,
    and refuse silent restatements of RAW close on recent shared dates. Adjusted
    close is exempt — it legitimately restates on every distribution. A genuine
    corporate action (e.g. a split) WILL trip the raw-close check: that is the
    intended behaviour, because the ledger units would need restating too, so a
    loud stop and a human look is exactly right.
    """
    errs = []
    if not old_rows:
        return errs
    if len(new_rows) < len(old_rows):
        errs.append(f"new frame has {len(new_rows)} rows < existing {len(old_rows)}")
    if new_rows and old_rows and new_rows[-1][0] < old_rows[-1][0]:
        errs.append(f"new last date {new_rows[-1][0]} earlier than existing {old_rows[-1][0]}")
    old_close = {r[0]: r[4] for r in old_rows}
    shared = [r for r in new_rows if r[0] in old_close][-OVERLAP_DAYS:]
    for r in shared:
        oc = old_close[r[0]]
        if oc and abs(r[4] / oc - 1.0) > OVERLAP_TOL:
            errs.append(
                f"{r[0]}: raw close restated {oc} -> {r[4]} (>{OVERLAP_TOL:.1%}) — "
                f"possible identity switch or corporate action; manual review needed")
    return errs


def validate_ledger(rows, price_lookup):
    """rows: transaction dicts, chronological. price_lookup: date -> price row.

    Every date must be a real SGX trading date (the price index IS the calendar).
    Non-provisional BUY/SELL prices must lie within that day's low-high range.
    """
    errs = []
    units_running = 0
    last_date = None
    for i, r in enumerate(rows):
        tag = f"ledger[{i}]"
        t = r.get("type")
        if t not in VALID_TYPES:
            errs.append(f"{tag}: bad type {t!r}")
            continue
        d = r.get("date")
        if not isinstance(d, str) or d not in price_lookup:
            errs.append(f"{tag}: date {d!r} is not an SGX trading date in the price index")
            continue
        if last_date and d < last_date:
            errs.append(f"{tag}: rows not in chronological order")
        last_date = d
        u = r.get("units")
        if not isinstance(u, int) or isinstance(u, bool) or u <= 0:
            errs.append(f"{tag}: units must be a positive integer")
            u = 0
        prov = bool(r.get("provisional"))
        if t in ("BUY", "SELL"):
            p = r.get("price")
            if p is None:
                if not prov:
                    errs.append(f"{tag}: null price only allowed on provisional rows")
            elif not isinstance(p, (int, float)) or p <= 0:
                errs.append(f"{tag}: price not positive ({p!r})")
            elif not prov:
                _d, _o, hi, lo, _c, _ac = price_lookup[d]
                if not (lo <= p <= hi):
                    errs.append(f"{tag}: price {p} outside {d} range {lo}-{hi}")
            f = r.get("fees")
            if f is not None and (not isinstance(f, (int, float)) or f < 0):
                errs.append(f"{tag}: fees must be >= 0")
            units_running += u if t == "BUY" else -u
            if units_running < 0:
                errs.append(f"{tag}: cumulative units go negative")
        elif t == "DIV":
            dps, amt = r.get("dps"), r.get("amount")
            ok_dps = isinstance(dps, (int, float)) and dps > 0
            ok_amt = isinstance(amt, (int, float)) and amt > 0
            if not (ok_dps or ok_amt):
                errs.append(f"{tag}: DIV needs dps or amount > 0")
    return errs
