"""supertree engine — valuation, decomposition, XIRR, drawdowns, milestones.

Pure stdlib, deterministic, loud on error. Binding rules (project CLAUDE.md):
- Valuation = units x RAW close. Adjusted close NEVER enters the valuation path;
  it is used only for the long-history education series, clearly labelled.
- Dividends are separate cash rows (assumption A2: received as cash, not
  reinvested). Total outcome = value + dividends - money in. No double count.
- provisional:true on any ledger row propagates to every derived figure.
"""
from datetime import date  # Python datetime: months are 1-indexed

DD_EPISODE_THRESHOLD = -0.20  # education layer: falls of 20%+ count as episodes
DD5 = -0.05                   # milestone: first 5% drawdown experienced/survived

# Forward-view bands — nominal SGD total-return CAGR. SIGNED OFF by ZH 2026-07-18.
# Provenance (all computed from data/prices.json, Yahoo ES3.SI adjusted close):
#   low  0.040  cautious haircut below the full-cycle total return.
#   mid  0.051  STI total-return CAGR since 2008 (education.long_run, 18.5y,
#               includes the full GFC cycle) — the conservative full-cycle base.
#   high 0.080  moderated optimistic; deliberately BELOW the 10.4% (last 10y)
#               and 17.0% (last 5y) windows so the band is not boom-anchored.
PROJECTION_BANDS = {"low": 0.040, "mid": 0.051, "high": 0.080}
PROJECTION_YEARS = 10  # round horizon; NO age or birth-year ever reaches the page


class EngineError(Exception):
    pass


def _d(iso):
    # date.fromisoformat expects YYYY-MM-DD; months 1-indexed in the date() ctor
    return date.fromisoformat(iso)


def build(prices, ledger_rows):
    """prices: {'cols': [...], 'rows': [[date,o,h,l,close,adjclose],...]}
    ledger_rows: list of transaction dicts, chronological.
    Returns (portfolio_dict, education_dict). Raises EngineError on any
    inconsistency — a wrong number shown confidently is the failure mode."""
    rows = prices["rows"]
    if not rows:
        raise EngineError("no price rows")
    dates = [r[0] for r in rows]
    close = {r[0]: r[4] for r in rows}
    date_ix = {d: i for i, d in enumerate(dates)}

    txns = sorted(ledger_rows, key=lambda r: r["date"])
    if not txns:
        raise EngineError("empty ledger")
    for t in txns:
        if t["date"] not in date_ix:
            raise EngineError(f"ledger date {t['date']} not in price index")

    # --- reconstruct units + cashflows -----------------------------------
    provisional = any(bool(t.get("provisional")) for t in txns)
    first_buy = min(t["date"] for t in txns if t["type"] == "BUY")
    buys, divs, flows = [], [], []  # flows: (date, +/- cash) for XIRR
    running = 0
    for t in txns:
        d, ty, u = t["date"], t["type"], t["units"]
        prov = bool(t.get("provisional"))
        if ty == "BUY":
            price = t["price"] if t["price"] is not None else close[d]
            fees = t["fees"] if t["fees"] is not None else 0.0
            cost = u * price + fees
            running += u
            buys.append({"date": d, "units": u, "price": round(price, 4),
                         "cost": round(cost, 2), "provisional": prov})
            flows.append((d, -cost))
        elif ty == "SELL":
            price = t["price"] if t["price"] is not None else close[d]
            fees = t["fees"] if t["fees"] is not None else 0.0
            running -= u
            if running < 0:
                raise EngineError(f"units negative after SELL on {d}")
            flows.append((d, u * price - fees))
        elif ty == "DIV":
            # DIV entitlement check: row units must equal units held before ex-date
            held = _units_before(txns, d)
            if u != held:
                raise EngineError(
                    f"DIV on {d} claims {u} units but {held} were held — fix the ledger")
            amount = t["amount"] if t.get("amount") is not None else u * t["dps"]
            divs.append({"date": d, "amount": round(amount, 2), "provisional": prov})
            flows.append((d, amount))

    # --- daily series from first buy -------------------------------------
    start_ix = date_ix[first_buy]
    s_dates, s_value, s_outcome, s_invested = [], [], [], []
    cum_div = 0.0
    cum_invested = 0.0
    div_by_date = {}
    for x in divs:
        div_by_date[x["date"]] = div_by_date.get(x["date"], 0.0) + x["amount"]
    buy_cost_by_date = {}
    for b in buys:
        buy_cost_by_date[b["date"]] = buy_cost_by_date.get(b["date"], 0.0) + b["cost"]
    units_series = _units_timeline(txns, dates[start_ix:])
    for i, d in enumerate(dates[start_ix:]):
        cum_div += div_by_date.get(d, 0.0)
        cum_invested += buy_cost_by_date.get(d, 0.0)
        v = units_series[i] * close[d]
        s_dates.append(d)
        s_value.append(round(v, 2))
        s_outcome.append(round(v + cum_div, 2))
        s_invested.append(round(cum_invested, 2))

    as_of = dates[-1]
    units_now = units_series[-1]
    value_now = units_now * close[as_of]
    invested = sum(b["cost"] for b in buys)  # SELLs not netted here; none exist yet
    div_total = sum(x["amount"] for x in divs)
    market_gain = value_now - invested
    outcome = value_now + div_total - invested
    if abs((market_gain + div_total) - outcome) > 1e-6:
        raise EngineError("decomposition identity failed")
    if abs(value_now - units_now * close[as_of]) > 1e-9:
        raise EngineError("valuation self-check failed")

    # --- risk stats on the outcome series (value + cash dividends) -------
    risk = _risk_stats(s_dates, s_outcome)

    # --- milestones -------------------------------------------------------
    milestones = _milestones(s_dates, s_outcome, s_invested, divs, risk)

    flows.append((as_of, value_now))
    mwr = _xirr(flows)

    portfolio = {
        "as_of": as_of,
        "ticker": prices["ticker"],
        "provisional": provisional,
        "position": {"units": units_now, "first_buy_date": first_buy,
                     "last_close": close[as_of], "value": round(value_now, 2)},
        "money": {"invested": round(invested, 2),
                  "dividends_received": round(div_total, 2),
                  "market_gain": round(market_gain, 2),
                  "total_outcome": round(outcome, 2),
                  "total_outcome_pct": round(outcome / invested, 6) if invested else None},
        "mwr_annualised": mwr,
        "series": {"dates": s_dates, "value": s_value,
                   "outcome": s_outcome, "invested": s_invested},
        "events": {"buys": buys, "divs": divs},
        "risk": risk,
        "trend": _trend(rows),
        "projection": _projection(value_now, as_of),
        "milestones": milestones,
    }
    education = _education(rows)
    return portfolio, education


def _units_before(txns, iso_date):
    """Units held strictly before iso_date (ex-date entitlement)."""
    running = 0
    for t in txns:
        if t["date"] >= iso_date:
            break
        if t["type"] == "BUY":
            running += t["units"]
        elif t["type"] == "SELL":
            running -= t["units"]
    return running


def _units_timeline(txns, window_dates):
    """Units held at each date in window_dates (inclusive of that date's buys)."""
    out, running, j = [], 0, 0
    txs = sorted(txns, key=lambda r: r["date"])
    for d in window_dates:
        while j < len(txs) and txs[j]["date"] <= d:
            if txs[j]["type"] == "BUY":
                running += txs[j]["units"]
            elif txs[j]["type"] == "SELL":
                running -= txs[j]["units"]
            j += 1
        out.append(running)
    return out


def _risk_stats(s_dates, series):
    peak = series[0]
    peak_date = s_dates[0]
    max_dd, max_dd_peak, max_dd_trough = 0.0, s_dates[0], s_dates[0]
    recovered = None
    for d, v in zip(s_dates, series):
        if v > peak:
            peak, peak_date = v, d
        dd = v / peak - 1.0 if peak else 0.0
        if dd < max_dd:
            max_dd, max_dd_peak, max_dd_trough = dd, peak_date, d
            recovered = None
        if recovered is None and max_dd < 0 and v >= _peak_value(s_dates, series, max_dd_peak):
            recovered = d
    current_dd = series[-1] / max(series) - 1.0 if series else 0.0
    worst = {"day": _worst_change(s_dates, series, 1),
             "5d": _worst_change(s_dates, series, 5),
             "21d": _worst_change(s_dates, series, 21)}
    return {"max_dd": round(max_dd, 6),
            "max_dd_peak": max_dd_peak, "max_dd_trough": max_dd_trough,
            "max_dd_recovered": recovered,
            "current_dd": round(current_dd, 6),
            "worst_day": worst["day"], "worst_5d": worst["5d"], "worst_21d": worst["21d"]}


def _peak_value(s_dates, series, peak_date):
    return series[s_dates.index(peak_date)]


def _worst_change(s_dates, series, n):
    """Worst n-trading-day change. 5 ~ a week, 21 ~ a month (trading days)."""
    worst, when = 0.0, None
    for i in range(n, len(series)):
        if series[i - n] <= 0:
            continue
        chg = series[i] / series[i - n] - 1.0
        if chg < worst:
            worst, when = chg, s_dates[i]
    return {"pct": round(worst, 6), "end_date": when}


def _trend(rows, window_months=15, sma_window=200):
    """Price-trend block for the 'look closer' layer: RAW close and its
    200-TRADING-day simple moving average, sliced to the last ~15 months.

    BINDING: the SMA is computed on r[4] (raw close) only — adjclose (r[5])
    never enters here, and this block never feeds the valuation path. The
    average spans 200 *trading* rows, not calendar days, via a rolling sum.
    SMA[i] is null until 200 rows exist (i >= sma_window-1)."""
    dates = [r[0] for r in rows]
    close = [r[4] for r in rows]  # RAW close — NOT adjclose
    n = len(rows)
    sma = [None] * n
    run = 0.0
    for i in range(n):
        run += close[i]
        if i >= sma_window:            # drop the row that fell out of the window
            run -= close[i - sma_window]
        if i >= sma_window - 1:        # full 200-row window available
            sma[i] = round(run / sma_window, 4)

    # cutoff = window_months before the last trading date. Python date months
    # are 1-indexed; work in a 0-indexed month count then convert back.
    last = _d(dates[-1])
    months0 = last.year * 12 + (last.month - 1) - window_months
    cy, cm0 = divmod(months0, 12)
    cutoff = date(cy, cm0 + 1, min(last.day, 28)).isoformat()  # clamp day, 1-indexed month
    keep = [i for i, d in enumerate(dates) if d >= cutoff]

    close_now, sma_now = close[-1], sma[-1]
    above = sma_now is not None and close_now >= sma_now
    distance = (close_now / sma_now - 1.0) if sma_now else None
    return {
        "sma_window": sma_window,
        "window_months": window_months,
        "as_of": dates[-1],
        "dates": [dates[i] for i in keep],
        "close": [round(close[i], 4) for i in keep],
        "sma": [sma[i] for i in keep],
        "close_now": round(close_now, 4),
        "sma_now": sma_now,
        "above": above,
        "distance_pct": round(distance, 6) if distance is not None else None,
    }


def _projection(value_now, as_of, bands=None, years=None):
    """Forward illustration: today's ETF value compounded at the signed-off
    low/mid/high total-return rates over a round horizon.

    BINDING: TRUE compound maths only — monthly rate m = (1+r)^(1/12)-1 and
    value = P*(1+m)^months. NEVER rule-of-72 / 2^doublings. Total-return basis
    assumes dividends are reinvested; this is a labelled illustration, not a
    promise. Emits one anchor point per year (months 0,12,...). The band must
    stay monotonic low<=mid<=high at every step (loud on violation)."""
    bands = PROJECTION_BANDS if bands is None else bands
    years = PROJECTION_YEARS if years is None else years
    y, mo, day = (int(x) for x in as_of.split("-"))  # ISO months 1-indexed
    P = float(value_now)
    dates = [date(y + k, mo, day).isoformat() for k in range(years + 1)]  # months 1-indexed in ctor
    series, ends = {}, {}
    for name, r in bands.items():
        m = (1.0 + r) ** (1.0 / 12.0) - 1.0
        series[name] = [round(P * (1.0 + m) ** (k * 12), 2) for k in range(years + 1)]
        ends[name] = series[name][-1]
    order = list(bands)  # rely on low/mid/high insertion order
    for k in range(years + 1):
        vals = [series[n][k] for n in order]
        if vals != sorted(vals):
            raise EngineError(f"projection band not monotonic at step {k}: {vals}")
    return {
        "basis": "total_return_reinvested_illustration",
        "start_value": round(P, 2),
        "start_date": as_of,
        "years": years,
        "rates": dict(bands),
        "dates": dates,
        "series": series,
        "end": {n: round(ends[n], 2) for n in order},
    }


def _milestones(s_dates, s_outcome, s_invested, divs, risk):
    first_div = divs[0] if divs else None
    growth10 = None
    for d, v, inv in zip(s_dates, s_outcome, s_invested):
        if inv > 0 and (v - inv) / inv >= 0.10:
            growth10 = d
            break
    dd5_exp = risk["max_dd"] <= DD5
    return {
        "first_dividend": {"achieved": first_div is not None,
                           "date": first_div["date"] if first_div else None,
                           "provisional": first_div["provisional"] if first_div else None},
        "growth_10pct": {"achieved": growth10 is not None, "date": growth10},
        "dd5_experienced": {"achieved": dd5_exp,
                            "trough_date": risk["max_dd_trough"] if dd5_exp else None},
        "dd5_survived": {"achieved": bool(dd5_exp and risk["max_dd_recovered"]),
                         "recovered_date": risk["max_dd_recovered"] if dd5_exp else None},
    }


def _xirr(flows, tol=1e-9, max_iter=100):
    """Money-weighted annualised return. flows: [(iso_date, cash), ...] with
    outflows negative. Newton with bisection fallback. None when undefined."""
    if len(flows) < 2:
        return None
    t0 = _d(flows[0][0])
    span_days = (_d(flows[-1][0]) - t0).days
    if span_days < 30:
        return None
    times = [(_d(d) - t0).days / 365.25 for d, _ in flows]
    cash = [c for _, c in flows]
    if not (any(c < 0 for c in cash) and any(c > 0 for c in cash)):
        return None

    def npv(r):
        return sum(c / (1.0 + r) ** t for c, t in zip(cash, times))

    lo, hi = -0.95, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < 1e-10:
            return round(mid, 6)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return round((lo + hi) / 2, 6)


def _education(rows):
    """Long-history context on ADJUSTED close (includes dividends) — clearly
    labelled, never used for valuation. Monthly series for the chart, episodes
    from daily. History starts where Yahoo depth starts; label honestly."""
    daily = [(r[0], r[5]) for r in rows]
    monthly = []
    for i, (d, v) in enumerate(daily):
        nxt = daily[i + 1][0][:7] if i + 1 < len(daily) else None
        if d[:7] != nxt:  # last trading day of its month
            monthly.append([d, round(v, 4)])
    episodes = _dd_episodes(daily)
    first_d, last_d = daily[0][0], daily[-1][0]
    years = (_d(last_d) - _d(first_d)).days / 365.25
    cagr = (daily[-1][1] / daily[0][1]) ** (1 / years) - 1 if years > 0 else None
    return {
        "basis": "adjclose_total_return_approx",
        "history_start": first_d, "history_end": last_d,
        "series_monthly": monthly,
        "episodes": episodes,
        "long_run": {"cagr_since_start": round(cagr, 6) if cagr is not None else None,
                     "years": round(years, 2),
                     "candidate_only": True,
                     "note": "NOT for display until ZH signs off projection bands."},
    }


def _dd_episodes(daily):
    """Falls of DD_EPISODE_THRESHOLD or worse: peak, trough, depth, recovery."""
    episodes = []
    peak_v, peak_d = daily[0][1], daily[0][0]
    in_ep, trough_v, trough_d, ep_peak_v, ep_peak_d = False, None, None, None, None
    for d, v in daily:
        if v >= peak_v:
            if in_ep:
                episodes.append(_ep(ep_peak_d, ep_peak_v, trough_d, trough_v, d))
                in_ep = False
            peak_v, peak_d = v, d
            continue
        dd = v / peak_v - 1.0
        if not in_ep and dd <= DD_EPISODE_THRESHOLD:
            in_ep, trough_v, trough_d = True, v, d
            ep_peak_v, ep_peak_d = peak_v, peak_d
        elif in_ep and v < trough_v:
            trough_v, trough_d = v, d
    if in_ep:
        episodes.append(_ep(ep_peak_d, ep_peak_v, trough_d, trough_v, None))
    episodes.sort(key=lambda e: e["depth_pct"])
    return episodes


def _ep(peak_d, peak_v, trough_d, trough_v, recover_d):
    days = (_d(recover_d) - _d(peak_d)).days if recover_d else None
    return {"peak_date": peak_d, "trough_date": trough_d,
            "depth_pct": round(trough_v / peak_v - 1.0, 6),
            "recover_date": recover_d, "days_peak_to_recover": days}
