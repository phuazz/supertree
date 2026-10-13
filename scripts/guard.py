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


def is_phantom_row(row, prev_row):
    """True for a non-trading placeholder: O=H=L=C, equal to the previous close."""
    if prev_row is None:
        return False
    _, o, h, low, c, _ = row
    return o == h == low == c and c == prev_row[4]


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
    # A date may leave the history only if the old row was a phantom holiday
    # print: flat bar (O=H=L=C) equal to the previous close. Yahoo carried such
    # rows on SGX holidays and later removed them (2026-10-09). Any other
    # missing date is lost history and fails.
    new_dates = {r[0] for r in new_rows}
    phantoms, lost = 0, []
    for i, r in enumerate(old_rows):
        if r[0] in new_dates:
            continue
        if is_phantom_row(r, old_rows[i - 1] if i else None):
            phantoms += 1
        else:
            lost.append(r[0])
    if lost:
        errs.append(f"{len(lost)} existing dates missing from new frame "
                    f"(first {lost[0]}, last {lost[-1]})")
    if len(new_rows) < len(old_rows) - phantoms:
        errs.append(f"new frame has {len(new_rows)} rows < existing {len(old_rows)} "
                    f"less {phantoms} phantom holiday rows")
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


def validate_ledger(rows, price_lookup, fund_lookups=None, main_ticker=None):
    """rows: transaction dicts, chronological. price_lookup: date -> price row of
    the main fund (its dates ARE the SGX trading calendar). fund_lookups:
    {ticker: date -> price row} for other funds traded in the ledger.

    Every date must be a real SGX trading date. Non-provisional BUY/SELL prices
    must lie within THAT fund's low-high range on that day. Units are tracked
    per fund, so a SELL can never exceed what is held of that fund.
    """
    errs = []
    held = {}
    last_date = None
    fund_lookups = fund_lookups or {}
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
        tk = r.get("ticker") or main_ticker
        if tk in fund_lookups:
            lookup = fund_lookups[tk]
        elif main_ticker is None or tk == main_ticker:
            lookup = price_lookup
        else:  # never check one fund's fill against another fund's prices
            errs.append(f"{tag}: no price data for {tk} to check this row against")
            continue
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
                if d not in lookup:
                    errs.append(f"{tag}: {tk} has no price row on {d} to check the fill against")
                else:
                    _d, _o, hi, lo, _c, _ac = lookup[d]
                    if not (lo <= p <= hi):
                        errs.append(f"{tag}: price {p} outside {tk} {d} range {lo}-{hi}")
            f = r.get("fees")
            if f is not None and (not isinstance(f, (int, float)) or f < 0):
                errs.append(f"{tag}: fees must be >= 0")
            fc = r.get("from_cash")
            if fc is not None and fc != "all" and (not isinstance(fc, (int, float)) or fc < 0):
                errs.append(f"{tag}: from_cash must be a number >= 0 or \"all\"")
            held[tk] = held.get(tk, 0) + (u if t == "BUY" else -u)
            if held[tk] < 0:
                errs.append(f"{tag}: units of {tk} go negative")
        elif t == "DIV":
            dps, amt = r.get("dps"), r.get("amount")
            ok_dps = isinstance(dps, (int, float)) and dps > 0
            ok_amt = isinstance(amt, (int, float)) and amt > 0
            if not (ok_dps or ok_amt):
                errs.append(f"{tag}: DIV needs dps or amount > 0")
    return errs


def validate_watch_frame(rows, old_rows, meta, expect_currency):
    """A watched (0-unit) fund's price frame: the same row and keep-last-good
    checks as the holding, plus an identity check — Yahoo must report the
    expected currency, so a mistyped or re-pointed symbol fails loudly
    instead of drawing some other instrument's prices."""
    errs = validate_price_rows(rows) + validate_replacement(rows, old_rows)
    cur = (meta or {}).get("currency")
    if cur != expect_currency:
        errs.append(f"currency {cur!r} != expected {expect_currency!r} — wrong symbol?")
    return errs
