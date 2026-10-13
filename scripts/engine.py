"""supertree engine — valuation, decomposition, XIRR, drawdowns, milestones.

Pure stdlib, deterministic, loud on error. Binding rules (project CLAUDE.md):
- Valuation = units x RAW close. Adjusted close NEVER enters the valuation path;
  it is used only for the long-history education series, clearly labelled.
- Dividends are separate cash rows (assumption A2: received as cash, not
  reinvested). Total outcome = value + dividends - money in. No double count.
- provisional:true on any ledger row propagates to every derived figure.
"""
import math
from datetime import date  # Python datetime: months are 1-indexed

DD_EPISODE_THRESHOLD = -0.20  # education layer: falls of 20%+ count as episodes
DD5 = -0.05                   # milestone: first 5% drawdown experienced/survived

# Forward view — lognormal "funnel". SIGNED OFF by ZH 2026-10-12 (replaces the
# fixed low/mid/high CAGR bands signed off 2026-07-18).
#   growth  ASSUMED, not from data: PROJECTION_ASSUMED_CAGR, ZH's forward view
#           that equities return about 6-8% a year nominal with dividends
#           reinvested (signed off 2026-10-12; 7% = middle of that range). The
#           page labels it a best guess and shows the data figures beside it.
#   sigma   computed on every build from data/prices.json (Yahoo ES3.SI
#           adjusted close): stdev of monthly log returns x sqrt(12).
# Data figures, also computed on every build and shown for comparison:
#   hist    growth from the FIRST row of history (2 Jan 2008, a pre-GFC peak).
#   rolling10  median / 1-in-10 / 9-in-10 growth over every real 10-year
#           stretch of month-end history (overlapping; few independent decades).
# Outcome at t years: P * exp(mu_log*t + z*sigma*sqrt(t)), z per percentile.
# Cross-checked against a 12-month block bootstrap of real history in tests.
FUNNEL_Z = {"p10": -1.2815516, "p50": 0.0, "p90": 1.2815516}
PROJECTION_ASSUMED_CAGR = 0.07            # ZH-signed assumption, see note above
PROJECTION_ASSUMED_RANGE = (0.06, 0.08)   # the range it is the middle of
PROJECTION_YEARS = 20          # longest horizon offered; NO age or birth-year ever reaches the page
PROJECTION_DEFAULT_YEARS = 10  # horizon shown on first load


class EngineError(Exception):
    pass


def _d(iso):
    # date.fromisoformat expects YYYY-MM-DD; months 1-indexed in the date() ctor
    return date.fromisoformat(iso)


def build(prices, ledger_rows, watch=None, frames=None):
    """prices: {'ticker', 'cols', 'rows': [[date,o,h,l,close,adjclose],...]} — the
    main fund. Its rows are the TRADING CALENDAR and feed trend, funnel and the
    since-2008 history (it has the deep history).
    ledger_rows: transaction dicts, chronological. A row's "ticker" defaults to
    the main fund's. BUY may carry "from_cash" (a number, or "all"): that part
    is paid from the cash account; the rest is NEW money put in.
    frames: price frames for other funds that may be held (e.g. GAB.SI).
    watch: price frames for funds shown with 0 units (compared, never valued).

    Money model (binding, see CLAUDE.md): valuation = units x RAW close per
    fund. Dividends and sale proceeds land in a CASH account (assumption A2:
    cash, not reinvested) and only leave it when a BUY says so. Money put in =
    sum of new money. Total outcome = holdings value + cash - money put in.
    Before any switch, cash == sum of dividends, so this is exactly
    value + dividends - money in (the double-count guard).
    Returns (portfolio_dict, education_dict). Raises EngineError on any
    inconsistency — a wrong number shown confidently is the failure mode."""
    rows = prices["rows"]
    if not rows:
        raise EngineError("no price rows")
    main_tk = prices["ticker"]
    dates = [r[0] for r in rows]
    date_ix = {d: i for i, d in enumerate(dates)}
    frame_rows = {main_tk: rows}
    for f in (frames or []):
        if f.get("rows"):
            frame_rows[f["ticker"]] = f["rows"]
    close_by = {k: {r[0]: r[4] for r in v} for k, v in frame_rows.items()}

    txns = [dict(t, ticker=t.get("ticker") or main_tk)
            for t in sorted(ledger_rows, key=lambda r: r["date"])]  # stable: same-day order kept
    if not txns:
        raise EngineError("empty ledger")
    for t in txns:
        if t["date"] not in date_ix:
            raise EngineError(f"ledger date {t['date']} not in price index")
        if t["ticker"] not in close_by:
            raise EngineError(f"no price frame for {t['ticker']} (ledger row {t['date']})")

    def trade_px(t):
        if t.get("price") is not None:
            return t["price"]
        px = close_by[t["ticker"]].get(t["date"])
        if px is None:
            raise EngineError(f"{t['ticker']} has no close on {t['date']} to stand in for a null price")
        return px

    # --- reconstruct units, cash and money in ------------------------------
    provisional = any(bool(t.get("provisional")) for t in txns)
    first_buy = min(t["date"] for t in txns if t["type"] == "BUY")
    buys, sells, divs, flows = [], [], [], []  # flows: (date, +/- cash) for XIRR
    units, basis, prov_tk = {}, {}, {}
    cash = 0.0
    cash_delta, new_by_date = {}, {}
    for t in txns:
        d, ty, u, tk = t["date"], t["type"], t["units"], t["ticker"]
        prov = bool(t.get("provisional"))
        prov_tk[tk] = prov_tk.get(tk, False) or prov
        if ty == "BUY":
            price = trade_px(t)
            fees = t["fees"] if t.get("fees") is not None else 0.0
            cost = u * price + fees
            fc = t.get("from_cash", 0) or 0
            fc = cash if fc == "all" else float(fc)
            if fc < 0 or fc > cash + 0.005:
                raise EngineError(f"BUY on {d} takes S${fc:.2f} from cash but only S${cash:.2f} is there")
            if fc > cost + 0.005:
                raise EngineError(f"BUY on {d} takes more from cash (S${fc:.2f}) than it costs (S${cost:.2f})")
            new = cost - fc
            cash -= fc
            cash_delta[d] = cash_delta.get(d, 0.0) - fc
            new_by_date[d] = new_by_date.get(d, 0.0) + new
            units[tk] = units.get(tk, 0) + u
            basis[tk] = basis.get(tk, 0.0) + cost
            buys.append({"date": d, "ticker": tk, "units": u, "price": round(price, 4),
                         "cost": round(cost, 2), "from_cash": round(fc, 2),
                         "new_money": round(new, 2), "provisional": prov})
            flows.append((d, -cost))
        elif ty == "SELL":
            price = trade_px(t)
            fees = t["fees"] if t.get("fees") is not None else 0.0
            held = units.get(tk, 0)
            if u > held:
                raise EngineError(f"SELL on {d} of {u} {tk} but only {held} held")
            proceeds = u * price - fees
            basis[tk] = basis.get(tk, 0.0) * (held - u) / held if held else 0.0  # average cost
            units[tk] = held - u
            cash += proceeds
            cash_delta[d] = cash_delta.get(d, 0.0) + proceeds
            sells.append({"date": d, "ticker": tk, "units": u, "price": round(price, 4),
                          "proceeds": round(proceeds, 2), "provisional": prov})
            flows.append((d, proceeds))
        elif ty == "DIV":
            # DIV entitlement check: row units must equal units of THAT fund held before ex-date
            held = _units_before(txns, d, tk)
            if u != held:
                raise EngineError(
                    f"DIV on {d} claims {u} units of {tk} but {held} were held — fix the ledger")
            amount = t["amount"] if t.get("amount") is not None else u * t["dps"]
            cash += amount
            cash_delta[d] = cash_delta.get(d, 0.0) + amount
            divs.append({"date": d, "ticker": tk, "amount": round(amount, 2), "provisional": prov})
            flows.append((d, amount))
    if cash < -0.005:
        raise EngineError(f"cash account went negative: S${cash:.2f}")

    # --- daily series from first buy -------------------------------------
    window = dates[date_ix[first_buy]:]
    tickers = sorted(units)
    unit_tl = {tk: _units_timeline(txns, window, tk) for tk in tickers}
    px_tl, px_date_tl = {}, {}
    for tk in tickers:  # each fund's close, carried forward over days it did not trade
        fr, j, last, last_d = frame_rows[tk], 0, None, None
        px_tl[tk], px_date_tl[tk] = [], []
        while j < len(fr) and fr[j][0] < window[0]:
            last, last_d = fr[j][4], fr[j][0]
            j += 1
        for d in window:
            while j < len(fr) and fr[j][0] <= d:
                last, last_d = fr[j][4], fr[j][0]
                j += 1
            px_tl[tk].append(last)
            px_date_tl[tk].append(last_d)
    s_dates, s_value, s_outcome, s_invested = [], [], [], []
    cum_cash = cum_new = 0.0
    for i, d in enumerate(window):
        cum_cash += cash_delta.get(d, 0.0)
        cum_new += new_by_date.get(d, 0.0)
        v = 0.0
        for tk in tickers:
            if unit_tl[tk][i]:
                if px_tl[tk][i] is None:
                    raise EngineError(f"{tk} held on {d} but has no price yet")
                v += unit_tl[tk][i] * px_tl[tk][i]
        s_dates.append(d)
        s_value.append(round(v, 2))
        s_outcome.append(round(v + cum_cash, 2))
        s_invested.append(round(cum_new, 2))

    as_of = dates[-1]
    holdings = []
    for tk in tickers:
        n = unit_tl[tk][-1]
        if not n:
            continue
        px = px_tl[tk][-1]
        holdings.append({"ticker": tk, "units": n, "last_close": px,
                         "last_price_date": px_date_tl[tk][-1],
                         "value": round(n * px, 2), "cost": round(basis[tk], 2),
                         "avg_cost": round(basis[tk] / n, 6), "provisional": prov_tk.get(tk, False)})
    value_now = sum(h["units"] * h["last_close"] for h in holdings)
    invested = sum(b["new_money"] for b in buys)
    div_total = sum(x["amount"] for x in divs)
    outcome = value_now + cash - invested
    market_gain = outcome - div_total
    # self-checks: the cash account must reconcile from its parts, and series end == totals
    recon = div_total + sum(x["proceeds"] for x in sells) - sum(b["from_cash"] for b in buys)
    if abs(recon - cash) > 0.02:
        raise EngineError(f"cash does not reconcile: {cash:.2f} vs {recon:.2f}")
    if abs(s_outcome[-1] - (value_now + cash)) > 0.02 or abs(s_invested[-1] - invested) > 0.02:
        raise EngineError("series end does not match totals")

    # --- risk stats on the outcome series (holdings + cash) ---------------
    risk = _risk_stats(s_dates, s_outcome)
    milestones = _milestones(s_dates, s_outcome, s_invested, divs, risk)

    flows.append((as_of, value_now))  # cash already left via DIV/SELL flows
    mwr = _xirr(flows)

    held_now = {h["ticker"] for h in holdings}
    portfolio = {
        "as_of": as_of,
        "ticker": main_tk,
        "provisional": provisional,
        "position": {"value": round(value_now, 2), "cash": round(cash, 2),
                     "first_buy_date": first_buy, "holdings": holdings},
        "money": {"invested": round(invested, 2),
                  "dividends_received": round(div_total, 2),
                  "cash": round(cash, 2),
                  "market_gain": round(market_gain, 2),
                  "total_outcome": round(outcome, 2),
                  "total_outcome_pct": round(outcome / invested, 6) if invested else None},
        "mwr_annualised": mwr,
        "series": {"dates": s_dates, "value": s_value,
                   "outcome": s_outcome, "invested": s_invested},
        "events": {"buys": buys, "sells": sells, "divs": divs},
        "risk": risk,
        "trend": _trend(rows),
        "projection": (_projection(value_now, as_of, fp, cagr=PROJECTION_ASSUMED_CAGR)
                       if (fp := _funnel_params(rows)) else None),
        "milestones": milestones,
        "watch": [_watch(w, rows) for w in (watch or []) if w.get("ticker") not in held_now],
    }
    education = _education(rows)
    return portfolio, education


def _watch(frame, hold_rows):
    """A watched fund: last price and growth, each against the HOLDING over the
    exact same days. Both sides use ADJUSTED close (dividends included) so an
    accumulating fund is compared like for like with a paying-out one. Not a
    valuation: units are 0 and nothing here enters money, risk or milestones."""
    rows = frame.get("rows") or []
    out = {"ticker": frame.get("ticker"), "label": frame.get("label"), "units": 0,
           "has_data": bool(rows)}
    if not rows:
        return out
    first_d, last_d = rows[0][0], rows[-1][0]
    out.update({"first_date": first_d, "last_date": last_d, "last_close": rows[-1][4]})
    w_adj = {r[0]: r[5] for r in rows}
    h_adj = {r[0]: r[5] for r in hold_rows}

    def window(start_iso):
        # common trading days of both funds inside [start, last]
        common = [d for d in (r[0] for r in rows) if d >= start_iso and d in h_adj]
        if len(common) < 2:
            return None
        a, b = common[0], common[-1]
        return {"from": a, "to": b,
                "watch": round(w_adj[b] / w_adj[a] - 1.0, 6),
                "holding": round(h_adj[b] / h_adj[a] - 1.0, 6)}

    out["since_listing"] = window(first_d)
    ld = _d(last_d)
    try:
        yr_ago = date(ld.year - 1, ld.month, ld.day).isoformat()  # months 1-indexed in ctor
    except ValueError:  # 29 Feb -> 28 Feb a year earlier
        yr_ago = date(ld.year - 1, ld.month, 28).isoformat()
    out["one_year"] = window(yr_ago) if first_d <= yr_ago else None
    return out


def _units_before(txns, iso_date, ticker=None):
    """Units of one fund held strictly before iso_date (ex-date entitlement)."""
    running = 0
    for t in txns:
        if t["date"] >= iso_date:
            break
        if ticker is not None and t.get("ticker") != ticker:
            continue
        if t["type"] == "BUY":
            running += t["units"]
        elif t["type"] == "SELL":
            running -= t["units"]
    return running


def _units_timeline(txns, window_dates, ticker=None):
    """Units of one fund held at each date (inclusive of that date's trades)."""
    out, running, j = [], 0, 0
    txs = sorted(txns, key=lambda r: r["date"])
    for d in window_dates:
        while j < len(txs) and txs[j]["date"] <= d:
            t = txs[j]
            if ticker is None or t.get("ticker") == ticker:
                if t["type"] == "BUY":
                    running += t["units"]
                elif t["type"] == "SELL":
                    running -= t["units"]
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


def _funnel_params(rows):
    """Funnel parameters from ADJUSTED close (total return, dividends in).
    mu_log runs from the first row of history (conservative start, see
    FUNNEL_Z note) to the last; sigma from month-end log returns. Forward
    illustration only — never enters the valuation path."""
    first_d, last_d = rows[0][0], rows[-1][0]
    years = (_d(last_d) - _d(first_d)).days / 365.25
    if years < 2:
        return None  # too little history to estimate a spread; the page hides the funnel
    mu_log = math.log(rows[-1][5] / rows[0][5]) / years
    month_end = [r[5] for i, r in enumerate(rows)
                 if i + 1 == len(rows) or rows[i + 1][0][:7] != r[0][:7]]
    lr = [math.log(month_end[i] / month_end[i - 1]) for i in range(1, len(month_end))]
    if len(lr) < 12:
        return None
    mean = sum(lr) / len(lr)
    sigma = math.sqrt(sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)) * math.sqrt(12.0)
    # every real 10-year stretch (120 month-ends apart), one per starting month
    ten = sorted((month_end[i + 120] / month_end[i]) ** 0.1 - 1.0 for i in range(len(month_end) - 120))
    rolling10 = None
    if ten:
        n = len(ten)
        med = ten[n // 2] if n % 2 else (ten[n // 2 - 1] + ten[n // 2]) / 2.0
        rolling10 = {"median": round(med, 6), "p10": round(ten[int(0.1 * n)], 6),
                     "p90": round(ten[int(0.9 * n)], 6), "count": n}
    return {"mu_log": mu_log, "sigma": sigma, "rolling10": rolling10,
            "history_start": first_d, "history_end": last_d, "months": len(lr)}


def _projection(value_now, as_of, params, years=None, cagr=None):
    """Forward funnel: P10 / median / P90 of a lognormal outcome for today's
    ETF value, one anchor per year (0..years).

    BINDING: value_t = P * exp(mu_log*t + z*sigma*sqrt(t)). The median is true
    compounding at CAGR = e^mu_log - 1 (never rule-of-72). Total-return basis
    assumes dividends are reinvested; a labelled illustration, not a promise.
    The page re-runs the same maths for any start amount, horizon and monthly
    top-up; with no top-up and today's value it must reproduce these series.
    Loud if the band is not ordered p10<=p50<=p90 at every step."""
    years = PROJECTION_YEARS if years is None else years
    # growth: the signed-off assumption when given, else the history rate
    mu = math.log(1.0 + cagr) if cagr is not None else params["mu_log"]
    sig = params["sigma"]
    y, mo, day = (int(x) for x in as_of.split("-"))  # ISO months 1-indexed
    day = min(day, 28)  # clamp so +k years never lands on 29 Feb of a non-leap year
    P = float(value_now)
    dates = [date(y + k, mo, day).isoformat() for k in range(years + 1)]  # months 1-indexed in ctor
    series = {n: [round(P * math.exp(mu * k + z * sig * math.sqrt(k)), 2)
                  for k in range(years + 1)]
              for n, z in FUNNEL_Z.items()}
    for k in range(years + 1):
        vals = [series[n][k] for n in FUNNEL_Z]
        if vals != sorted(vals):
            raise EngineError(f"funnel not ordered p10<=p50<=p90 at step {k}: {vals}")
    return {
        "basis": "lognormal_funnel_total_return_illustration",
        "start_value": round(P, 2),
        "start_date": as_of,
        "years": years,
        "default_years": min(PROJECTION_DEFAULT_YEARS, years),
        "mu_log": round(mu, 6),
        "cagr": round(math.exp(mu) - 1.0, 6),
        "sigma": round(sig, 6),
        "z": dict(FUNNEL_Z),
        "growth_basis": "assumed" if cagr is not None else "history",
        "assumed_range": list(PROJECTION_ASSUMED_RANGE) if cagr is not None else None,
        "hist_mu_log": round(params["mu_log"], 6),
        "hist_cagr": round(math.exp(params["mu_log"]) - 1.0, 6),
        "rolling10": params.get("rolling10"),
        "history_start": params["history_start"],
        "history_end": params["history_end"],
        "dates": dates,
        "series": series,
        "end": {n: series[n][-1] for n in FUNNEL_Z},
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
