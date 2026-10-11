"""Merge exchange-sourced rows for sessions Yahoo's history lacks.

data/price_patches.json holds RAW OHLC only, each row with its source. A patch
applies only where Yahoo has no row for that date. When Yahoo later supplies
the date, Yahoo wins provided its close agrees with the patch; a disagreement is
an error, because one of the two sources is wrong and a human should look.

Adjusted close is never stored in a patch: Yahoo restates adjclose on every
distribution, so a stored figure would go stale. It is derived at merge time
from the adjclose/close factor of the nearest later Yahoo row (the earlier row
when the patch is the newest date). A distribution between the patch date and
that row would make the factor wrong, so that case is refused.
"""
from datetime import date  # Python datetime: months are 1-indexed

from guard import OVERLAP_TOL


def validate_patch(p):
    errs = []
    d = p.get("date", "")
    try:
        wd = date.fromisoformat(d).weekday()  # Mon=0 .. Sun=6
    except ValueError:
        return [f"patch {d!r}: not an ISO date"]
    if wd >= 5:
        errs.append(f"patch {d}: falls on a weekend")
    o, h, low, c = (p.get(k) for k in ("open", "high", "low", "close"))
    if not all(isinstance(v, (int, float)) and v > 0 for v in (o, h, low, c)):
        errs.append(f"patch {d}: OHLC must be positive numbers")
    elif not (low <= min(o, c) and max(o, c) <= h):
        errs.append(f"patch {d}: OHLC inconsistent (low/high do not bracket open/close)")
    if not p.get("source"):
        errs.append(f"patch {d}: no source")
    return errs


def apply_patches(rows, patches, divs=()):
    """rows: Yahoo [date, o, h, l, c, adj], sorted. Returns (rows, errs, notes)."""
    errs, notes = [], []
    by_date = {r[0]: r for r in rows}
    div_dates = sorted(x["exdate"] for x in divs)
    out = list(rows)
    for p in patches:
        e = validate_patch(p)
        if e:
            errs += e
            continue
        d = p["date"]
        if d in by_date:
            yc = by_date[d][4]
            if abs(yc / p["close"] - 1.0) > OVERLAP_TOL:
                errs.append(f"patch {d}: Yahoo now has close {yc}, patch says {p['close']} "
                            f"(>{OVERLAP_TOL:.1%}) — sources disagree; manual review needed")
            else:
                notes.append(f"patch {d}: Yahoo now supplies this date; patch redundant, Yahoo kept")
            continue
        later = [r for r in rows if r[0] > d]
        earlier = [r for r in rows if r[0] < d]
        ref = later[0] if later else (earlier[-1] if earlier else None)
        if ref is None:
            errs.append(f"patch {d}: no Yahoo row to derive adjclose from")
            continue
        lo, hi = sorted((d, ref[0]))
        if any(lo < x <= hi for x in div_dates):
            errs.append(f"patch {d}: distribution between patch and reference row {ref[0]}; "
                        f"adjclose factor would be wrong")
            continue
        factor = ref[5] / ref[4]
        out.append([d, p["open"], p["high"], p["low"], p["close"],
                    round(p["close"] * factor, 4)])
        notes.append(f"patch {d}: inserted from {p['source'].split()[0]}")
    out.sort(key=lambda r: r[0])
    return out, errs, notes
