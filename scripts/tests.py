"""supertree test suite — zero-dependency, run with: python scripts/tests.py

Covers the four declared silent-failure modes: bad fetch overwriting good data,
dividend double-count, date/boundary drift, provisional flags not propagating.
The Actions refresh runs this FIRST and stops on failure (guard before cron).
"""
import math
import sys
from datetime import date, timedelta  # Python datetime: months are 1-indexed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import engine
import guard

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(name)


def mkrows(pairs, adj=None):
    """pairs: [(iso_date, close)] -> 6-field price rows with a small o/h/l band."""
    rows = []
    for i, (d, c) in enumerate(pairs):
        a = adj[i] if adj else c
        rows.append([d, round(c * 0.999, 4), round(c * 1.01, 4),
                     round(c * 0.99, 4), c, a])
    return rows


# ---------------------------------------------------------------- dates ----
def test_iso_ordering():
    # The whole codebase compares ISO strings; prove lexicographic == chronological
    check("iso: year boundary orders", "2025-12-31" < "2026-01-02")
    check("iso: month boundary orders", "2026-02-28" < "2026-03-02")
    check("iso: month 9 vs 10 orders", "2026-09-30" < "2026-10-01")


def test_year_boundary():
    # Buy on the last trading day of 2025; series must cross into 2026 cleanly
    pairs = [("2025-12-29", 4.00), ("2025-12-30", 4.02), ("2025-12-31", 4.04),
             ("2026-01-02", 4.10), ("2026-01-05", 4.06)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2025-12-31", "type": "BUY", "ticker": "T", "units": 100,
               "price": 4.04, "fees": 0, "provisional": False}]
    p, _ = engine.build(prices, ledger)
    check("year boundary: series spans it",
          p["series"]["dates"] == ["2025-12-31", "2026-01-02", "2026-01-05"])
    check("year boundary: value after boundary", p["series"]["value"][1] == 410.0,
          f'got {p["series"]["value"][1]}')
    check("year boundary: as_of", p["as_of"] == "2026-01-05")


def test_month_boundary():
    # Feb 2026 has 28 days (not a leap year); Feb -> Mar must not drop or dup
    pairs = [("2026-02-26", 5.00), ("2026-02-27", 5.10),
             ("2026-03-02", 5.20), ("2026-03-03", 5.15)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2026-02-26", "type": "BUY", "ticker": "T", "units": 10,
               "price": 5.00, "fees": 0, "provisional": False},
              {"date": "2026-03-02", "type": "DIV", "ticker": "T", "units": 10,
               "dps": 0.10, "amount": None, "provisional": False}]
    p, edu = engine.build(prices, ledger)
    check("month boundary: 4 series points", len(p["series"]["dates"]) == 4)
    check("month boundary: div lands in March",
          p["events"]["divs"][0]["date"] == "2026-03-02")
    check("month boundary: outcome adds div once",
          abs(p["series"]["outcome"][2] - (52.0 + 1.0)) < 1e-9,
          f'got {p["series"]["outcome"][2]}')
    months = [m[0] for m in edu["series_monthly"]]
    check("month boundary: monthly downsample picks month-ends",
          months == ["2026-02-27", "2026-03-03"], f"got {months}")


# ---------------------------------------------------------------- guard ----
def test_guard_price_frame():
    good = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1)])
    check("guard: clean frame passes", guard.validate_price_rows(good) == [])
    check("guard: empty frame fails", guard.validate_price_rows([]) != [])
    bad_order = mkrows([("2026-01-05", 4.0), ("2026-01-02", 4.1)])
    check("guard: unsorted dates fail", guard.validate_price_rows(bad_order) != [])
    bad_neg = mkrows([("2026-01-02", 4.0), ("2026-01-05", -1.0)])
    check("guard: negative close fails", guard.validate_price_rows(bad_neg) != [])
    jump = mkrows([("2026-01-02", 4.0), ("2026-01-05", 5.5)])  # +37.5% in a day
    check("guard: implausible daily move fails", guard.validate_price_rows(jump) != [])


def test_guard_replacement():
    old = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1), ("2026-01-06", 4.2)])
    fresh = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1),
                    ("2026-01-06", 4.2), ("2026-01-07", 4.25)])
    check("guard: longer fresher pull passes",
          guard.validate_replacement(fresh, old) == [])
    shorter = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1)])
    check("guard: shorter pull fails", guard.validate_replacement(shorter, old) != [])
    restated = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1),
                       ("2026-01-06", 4.2), ("2026-01-07", 4.25)])
    restated[1][4] = 3.9  # raw close silently changed on a shared date
    check("guard: raw close restatement fails",
          guard.validate_replacement(restated, old) != [])
    adj_moved = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1),
                        ("2026-01-06", 4.2), ("2026-01-07", 4.25)],
                       adj=[3.0, 3.1, 3.2, 3.25])  # adjclose restates: allowed
    check("guard: adjclose restatement allowed",
          guard.validate_replacement(adj_moved, old) == [])
    check("guard: no old data passes trivially",
          guard.validate_replacement(fresh, []) == [])
    # Phantom holiday row: flat bar equal to the previous close, later removed upstream
    with_phantom = mkrows([("2026-04-30", 4.0), ("2026-05-01", 4.0), ("2026-05-04", 4.1)])
    with_phantom[1][1:5] = [4.0, 4.0, 4.0, 4.0]
    cleaned = mkrows([("2026-04-30", 4.0), ("2026-05-04", 4.1), ("2026-05-05", 4.2)])
    check("guard: dropped phantom holiday row passes",
          guard.validate_replacement(cleaned, with_phantom) == [])
    real_lost = mkrows([("2026-04-30", 4.0), ("2026-05-04", 4.1), ("2026-05-05", 4.2)])
    old_real = mkrows([("2026-04-30", 4.0), ("2026-05-01", 4.05), ("2026-05-04", 4.1)])
    check("guard: dropped real trading row fails even when new frame is as long",
          guard.validate_replacement(real_lost, old_real) != [])
    flat_moved = [r[:] for r in with_phantom]
    flat_moved[1][1:5] = [4.02, 4.02, 4.02, 4.02]  # flat but not at previous close
    check("guard: dropped flat row off previous close fails",
          guard.validate_replacement(cleaned, flat_moved) != [])


def test_guard_ledger():
    rows = mkrows([("2026-01-02", 4.0), ("2026-01-05", 4.1)])
    lookup = {r[0]: r for r in rows}
    ok = [{"date": "2026-01-02", "type": "BUY", "units": 100, "price": 4.0,
           "fees": 1.0, "provisional": False}]
    check("ledger: clean row passes", guard.validate_ledger(ok, lookup) == [])
    bad_date = [dict(ok[0], date="2026-01-03")]  # not a trading date
    check("ledger: non-trading date fails",
          guard.validate_ledger(bad_date, lookup) != [])
    null_np = [dict(ok[0], price=None)]
    check("ledger: null price on confirmed row fails",
          guard.validate_ledger(null_np, lookup) != [])
    null_prov = [dict(ok[0], price=None, provisional=True)]
    check("ledger: null price on provisional row passes",
          guard.validate_ledger(null_prov, lookup) == [])
    out_range = [dict(ok[0], price=5.0)]  # day's range is ~3.96-4.04
    check("ledger: price outside day range fails",
          guard.validate_ledger(out_range, lookup) != [])
    oversell = ok + [{"date": "2026-01-05", "type": "SELL", "units": 200,
                      "price": 4.1, "fees": 0, "provisional": False}]
    check("ledger: cumulative negative units fail",
          guard.validate_ledger(oversell, lookup) != [])
    bad_div = [{"date": "2026-01-05", "type": "DIV", "units": 100,
                "dps": None, "amount": None, "provisional": False}]
    check("ledger: DIV without dps or amount fails",
          guard.validate_ledger(bad_div, lookup) != [])


# --------------------------------------------------------------- engine ----
def test_engine_hand_computed():
    pairs = [("2026-01-02", 2.00), ("2026-02-02", 2.20), ("2026-03-02", 2.50)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2026-01-02", "type": "BUY", "units": 100, "price": 2.00,
               "fees": 5.0, "provisional": False},
              {"date": "2026-02-02", "type": "DIV", "units": 100, "dps": 0.10,
               "amount": None, "provisional": False}]
    p, _ = engine.build(prices, ledger)
    m = p["money"]
    check("engine: invested = cost + fees", m["invested"] == 205.0)
    check("engine: dividends", m["dividends_received"] == 10.0)
    check("engine: value", p["position"]["value"] == 250.0)
    check("engine: market gain", m["market_gain"] == 45.0)
    check("engine: total outcome", m["total_outcome"] == 55.0)
    check("engine: outcome pct",  # engine stores 6dp; tolerance matches that
          abs(m["total_outcome_pct"] - 55.0 / 205.0) < 5e-7)
    check("engine: not provisional", p["provisional"] is False)
    check("engine: outcome series ends at total",
          abs(p["series"]["outcome"][-1] - (250.0 + 10.0)) < 1e-9)
    check("engine: invested line steps at buy",
          p["series"]["invested"] == [205.0, 205.0, 205.0])


def test_adjclose_never_in_valuation():
    pairs = [("2026-01-02", 2.00), ("2026-01-05", 2.50)]
    prices = {"ticker": "T", "cols": [],
              "rows": mkrows(pairs, adj=[999.0, 999.0])}  # absurd adjclose
    ledger = [{"date": "2026-01-02", "type": "BUY", "units": 10, "price": 2.00,
               "fees": 0, "provisional": False}]
    p, _ = engine.build(prices, ledger)
    check("no-double-count: value uses RAW close only",
          p["position"]["value"] == 25.0, f'got {p["position"]["value"]}')


def test_div_entitlement_mismatch_raises():
    pairs = [("2026-01-02", 2.00), ("2026-01-05", 2.10)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2026-01-02", "type": "BUY", "units": 100, "price": 2.00,
               "fees": 0, "provisional": False},
              {"date": "2026-01-05", "type": "DIV", "units": 999, "dps": 0.10,
               "amount": None, "provisional": False}]
    try:
        engine.build(prices, ledger)
        check("engine: DIV units mismatch raises", False, "no exception")
    except engine.EngineError:
        check("engine: DIV units mismatch raises", True)


def test_provisional_propagates():
    pairs = [("2026-01-02", 2.00), ("2026-01-05", 2.10)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2026-01-02", "type": "BUY", "units": 10, "price": None,
               "fees": None, "provisional": True}]
    p, _ = engine.build(prices, ledger)
    check("provisional: flag propagates to portfolio", p["provisional"] is True)
    check("provisional: null price resolves to that day's close",
          p["events"]["buys"][0]["price"] == 2.00)


def test_xirr():
    d0 = date(2020, 1, 1)          # months 1-indexed in the date() constructor
    d1 = d0 + timedelta(days=731)  # ~2 years incl leap day
    t = 731 / 365.25
    expected = 1.21 ** (1 / t) - 1.0
    r = engine._xirr([(d0.isoformat(), -100.0), (d1.isoformat(), 121.0)])
    check("xirr: two-flow known case", abs(r - expected) < 1e-6,
          f"got {r}, expected {expected}")
    check("xirr: <30 days returns None",
          engine._xirr([("2026-01-02", -100.0), ("2026-01-20", 101.0)]) is None)


def test_milestones_and_risk():
    # 100 -> 110 (peak) -> 103.4 (-6% dd) -> 111 (recovered, +10% outcome)
    pairs = [("2026-01-02", 1.00), ("2026-01-05", 1.10),
             ("2026-01-06", 1.034), ("2026-01-07", 1.11)]
    prices = {"ticker": "T", "cols": [], "rows": mkrows(pairs)}
    ledger = [{"date": "2026-01-02", "type": "BUY", "units": 100, "price": 1.00,
               "fees": 0, "provisional": False}]
    p, _ = engine.build(prices, ledger)
    r = p["risk"]
    check("risk: max dd -6%", abs(r["max_dd"] - (1.034 / 1.10 - 1.0)) < 1e-9,
          f'got {r["max_dd"]}')
    check("risk: dd dates", (r["max_dd_peak"], r["max_dd_trough"]) ==
          ("2026-01-05", "2026-01-06"))
    check("risk: recovered date", r["max_dd_recovered"] == "2026-01-07")
    check("risk: worst day is the fall day",
          r["worst_day"]["end_date"] == "2026-01-06")
    ms = p["milestones"]
    check("milestone: dd5 experienced", ms["dd5_experienced"]["achieved"] is True)
    check("milestone: dd5 survived", ms["dd5_survived"]["achieved"] is True)
    check("milestone: growth 10pct fires at the 110 point",
          ms["growth_10pct"]["date"] == "2026-01-05")
    check("milestone: no dividend yet", ms["first_dividend"]["achieved"] is False)


def test_education_episodes():
    # 30% fall then full recovery -> exactly one episode; 15% fall -> none
    deep = [("2026-01-02", 100.0), ("2026-01-05", 70.0), ("2026-01-06", 101.0),
            ("2026-01-07", 102.0)]
    edu = engine._education(mkrows(deep))
    eps = edu["episodes"]
    check("education: one 30% episode", len(eps) == 1 and
          abs(eps[0]["depth_pct"] - (-0.30)) < 1e-9, f"got {eps}")
    check("education: recovery date", eps[0]["recover_date"] == "2026-01-06")
    shallow = [("2026-01-02", 100.0), ("2026-01-05", 85.0), ("2026-01-06", 101.0)]
    edu2 = engine._education(mkrows(shallow))
    check("education: 15% fall is not an episode", edu2["episodes"] == [])


def test_funnel_maths():
    # Known parameters: P=1,000, mu_log=0.05, sigma=0.16, 10 years.
    # median = 1000*e^0.5 = 1648.72; p90 = 1000*e^(0.5 + 1.2815516*0.16*sqrt(10))
    params = {"mu_log": 0.05, "sigma": 0.16, "history_start": "2008-01-02",
              "history_end": "2026-10-09", "months": 225}
    f = engine._projection(1000.0, "2026-10-09", params, years=10)
    s = f["series"]
    check("funnel: median = P*e^(mu*t) (true compounding)",
          abs(f["end"]["p50"] - 1648.72) < 0.01, f'got {f["end"]["p50"]}')
    p90 = 1000.0 * math.exp(0.5 + 1.2815516 * 0.16 * math.sqrt(10))
    check("funnel: p90 = P*e^(mu*t + z*sigma*sqrt(t))",
          abs(f["end"]["p90"] - p90) < 0.01, f'got {f["end"]["p90"]} vs {p90:.2f}')
    check("funnel: every line starts at the principal",
          s["p10"][0] == s["p50"][0] == s["p90"][0] == 1000.0)
    check("funnel: 11 yearly anchor points (0..10)",
          len(f["dates"]) == 11 and all(len(v) == 11 for v in s.values()))
    check("funnel: ordered p10<=p50<=p90 at every step",
          all(s["p10"][k] <= s["p50"][k] <= s["p90"][k] for k in range(11)))
    widths = [s["p90"][k] - s["p10"][k] for k in range(11)]
    check("funnel: dollar range widens every year",
          all(widths[k] < widths[k + 1] for k in range(10)))
    ann = [(s["p90"][k] / 1000.0) ** (1 / k) - (s["p10"][k] / 1000.0) ** (1 / k) for k in range(1, 11)]
    check("funnel: yearly-rate range narrows with time (the patience lesson)",
          all(ann[k] > ann[k + 1] for k in range(9)))
    g = engine._projection(3000.0, "2026-10-09", params, years=10)
    check("funnel: scales linearly with the start amount",
          abs(g["end"]["p10"] - 3 * f["end"]["p10"]) < 0.05)
    leap = engine._projection(1000.0, "2028-02-29", params, years=3)
    check("funnel: 29 Feb start does not crash on non-leap years",
          leap["dates"][1] == "2029-02-28", f'got {leap["dates"]}')


def test_funnel_params_from_data():
    # Steady 1%/month growth on adjclose -> sigma 0, mu_log = ln(last/first)/years.
    pairs, adj = [], []
    for k in range(37):
        y, m = 2020 + (k // 12), 1 + (k % 12)   # 1-indexed month
        nxt = date(y + (m // 12), (m % 12) + 1, 1)  # first of the following month
        last = nxt - timedelta(days=1)
        pairs.append((last.isoformat(), 10.0))
        adj.append(10.0 * 1.01 ** k)
    prm = engine._funnel_params(mkrows(pairs, adj))
    yrs = (date.fromisoformat(pairs[-1][0]) - date.fromisoformat(pairs[0][0])).days / 365.25
    check("funnel params: mu_log = ln(last/first)/years (first row = start)",
          abs(prm["mu_log"] - math.log(1.01 ** 36) / yrs) < 1e-9, f'got {prm["mu_log"]}')
    check("funnel params: constant growth -> sigma 0", prm["sigma"] < 1e-9, f'got {prm["sigma"]}')
    check("funnel params: uses ADJUSTED close (raw close flat here)", prm["mu_log"] > 0.1)
    check("funnel params: under 10 years of history -> no 10-year-stretch figures",
          prm["rolling10"] is None)
    f7 = engine._projection(1000.0, "2026-10-09", prm, years=10, cagr=0.07)
    check("funnel: an assumed growth rate drives the median (1000 * 1.07^10)",
          abs(f7["end"]["p50"] - 1000 * 1.07 ** 10) < 0.01 and f7["growth_basis"] == "assumed",
          f'got {f7["end"]["p50"]}')


def test_trend_sma():
    # Hand-computed 4-trading-day SMA on a known raw-close series.
    # closes 1..6; SMA[3]=(1+2+3+4)/4=2.5, SMA[4]=3.5, SMA[5]=4.5; [0..2]=None
    pairs = [("2026-01-02", 1.0), ("2026-01-05", 2.0), ("2026-01-06", 3.0),
             ("2026-01-07", 4.0), ("2026-01-08", 5.0), ("2026-01-09", 6.0)]
    rows = mkrows(pairs)
    tr = engine._trend(rows, window_months=600, sma_window=4)  # huge window: keep all
    check("trend: sma warms up as None then fills",
          tr["sma"] == [None, None, None, 2.5, 3.5, 4.5], f'got {tr["sma"]}')
    check("trend: raw close carried verbatim",
          tr["close"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    check("trend: current above sma", tr["above"] is True)
    check("trend: sma_now hand-value", tr["sma_now"] == 4.5)
    check("trend: distance = close/sma - 1",
          abs(tr["distance_pct"] - (6.0 / 4.5 - 1.0)) < 5e-7, f'got {tr["distance_pct"]}')

    # below-SMA case: a falling tail must flip the state
    pairs2 = [("2026-01-02", 6.0), ("2026-01-05", 5.0), ("2026-01-06", 4.0),
              ("2026-01-07", 3.0), ("2026-01-08", 2.0), ("2026-01-09", 1.0)]
    tr2 = engine._trend(mkrows(pairs2), window_months=600, sma_window=4)
    check("trend: current below sma", tr2["above"] is False and tr2["distance_pct"] < 0)


def test_trend_ignores_adjclose():
    # SMA must be RAW close only — absurd adjclose must not move it.
    pairs = [("2026-01-02", 2.0), ("2026-01-05", 2.0), ("2026-01-06", 2.0),
             ("2026-01-07", 2.0), ("2026-01-08", 2.0)]
    rows = mkrows(pairs, adj=[999.0, 999.0, 999.0, 999.0, 999.0])
    tr = engine._trend(rows, window_months=600, sma_window=4)
    check("trend: sma ignores adjclose (raw only)",
          tr["sma"][-1] == 2.0 and tr["close_now"] == 2.0, f'got {tr["sma"][-1]}')


def test_trend_window_slice():
    # 15-month window must drop rows older than the cutoff and keep the tail.
    pairs = [("2024-01-02", 3.0), ("2025-01-02", 3.5), ("2026-01-02", 4.0),
             ("2026-06-01", 4.5), ("2026-07-17", 5.0)]
    tr = engine._trend(mkrows(pairs), window_months=15, sma_window=2)
    # cutoff = 15 months before 2026-07-17 -> 2025-04-17; keeps the last three
    check("trend: window drops pre-cutoff rows",
          tr["dates"] == ["2026-01-02", "2026-06-01", "2026-07-17"],
          f'got {tr["dates"]}')


# ------------------------------------------------------------ real data ----
def test_price_patches():
    import patches
    yahoo = [["2025-10-23", 4.465, 4.478, 4.445, 4.466, 4.3208],
             ["2025-10-27", 4.49, 4.515, 4.49, 4.501, 4.3547]]
    p = {"date": "2025-10-24", "open": 4.47, "high": 4.5, "low": 4.47, "close": 4.48,
         "source": "SGX test"}
    out, errs, _ = patches.apply_patches(yahoo, [p])
    check("patch: inserted between neighbours, sorted",
          [r[0] for r in out] == ["2025-10-23", "2025-10-24", "2025-10-27"] and errs == [])
    check("patch: adjclose uses the next Yahoo row's factor",
          out[1][5] == round(4.48 * 4.3547 / 4.501, 4))
    check("patch: merged frame passes the price guard", guard.validate_price_rows(out) == [])
    same = yahoo[:1] + [["2025-10-24", 4.47, 4.5, 4.47, 4.481, 4.33]] + yahoo[1:]
    out2, errs2, notes2 = patches.apply_patches(same, [p])
    check("patch: Yahoo supplies the date and agrees -> Yahoo kept, no error",
          errs2 == [] and out2[1][4] == 4.481 and "redundant" in notes2[0])
    clash = yahoo[:1] + [["2025-10-24", 4.6, 4.7, 4.6, 4.65, 4.5]] + yahoo[1:]
    check("patch: Yahoo disagrees beyond tolerance -> error",
          patches.apply_patches(clash, [p])[1] != [])
    check("patch: distribution between patch and reference row -> error",
          patches.apply_patches(yahoo, [p], [{"exdate": "2025-10-27", "dps": 0.1}])[1] != [])
    check("patch: missing source -> error",
          patches.apply_patches(yahoo, [dict(p, source="")])[1] != [])
    check("patch: high below close -> error",
          patches.apply_patches(yahoo, [dict(p, high=4.475)])[1] != [])
    # weekday rule at month and year boundaries (verified with datetime)
    check("patch date: Fri 2025-10-31 (month end) accepted",
          patches.validate_patch(dict(p, date="2025-10-31")) == [])
    check("patch date: Sat 2025-11-01 (month start) rejected",
          patches.validate_patch(dict(p, date="2025-11-01")) != [])
    check("patch date: Wed 2025-12-31 (year end) accepted",
          patches.validate_patch(dict(p, date="2025-12-31")) == [])
    check("patch date: Sat 2027-01-02 (year start) rejected",
          patches.validate_patch(dict(p, date="2027-01-02")) != [])


def test_watch_fund():
    # Holding: steady 1%/day on adjclose over 6 days. Watch fund lists on day 3.
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    hold = mkrows([(d, 10.0) for d in days], [10.0 * 1.01 ** i for i in range(6)])
    wrows = mkrows([(d, 5.0 * 1.02 ** i) for i, d in enumerate(days[2:])])
    w = engine._watch({"ticker": "GAB.SI", "label": "x", "rows": wrows}, hold)
    sl = w["since_listing"]
    check("watch: compared over the SAME days (from listing to last)",
          sl["from"] == "2026-01-06" and sl["to"] == "2026-01-09", f"{sl}")
    check("watch: fund growth from its own adjusted close", abs(sl["watch"] - (1.02 ** 3 - 1)) < 1e-6)
    check("watch: holding growth over that window, dividends in (adjclose)",
          abs(sl["holding"] - (1.01 ** 3 - 1)) < 1e-6, f"{sl['holding']}")
    check("watch: under a year of history -> no one-year figure", w["one_year"] is None)
    check("watch: 0 units, never a valuation", w["units"] == 0 and "value" not in w)
    empty = engine._watch({"ticker": "GAB.SI", "rows": []}, hold)
    check("watch: no prices yet -> has_data False, nothing else claimed",
          empty["has_data"] is False and "last_close" not in empty)
    # a watched fund must not move a single money figure
    ledger = [{"date": days[0], "type": "BUY", "ticker": "ES3.SI", "units": 10,
               "price": 10.0, "fees": 1.0, "provisional": False}]
    prices = {"ticker": "ES3.SI", "cols": [], "rows": hold}
    a, _ = engine.build(prices, ledger)
    b, _ = engine.build(prices, ledger, watch=[{"ticker": "GAB.SI", "rows": wrows}])
    check("watch: adding a watched fund leaves money, position and risk unchanged",
          a["money"] == b["money"] and a["position"] == b["position"] and a["risk"] == b["risk"])
    # guard: identity + keep-last-good
    ok = guard.validate_watch_frame(wrows, [], {"currency": "SGD"}, "SGD")
    check("watch guard: good SGD frame passes", ok == [], f"{ok}")
    check("watch guard: wrong currency fails (wrong symbol)",
          any("currency" in e for e in guard.validate_watch_frame(wrows, [], {"currency": "USD"}, "SGD")))
    check("watch guard: a shorter pull than last-good fails",
          guard.validate_watch_frame(wrows[:2], wrows, {"currency": "SGD"}, "SGD") != [])


def test_switch_between_funds():
    # Fund T (main calendar) and fund U. Hand-checkable story:
    #  d0 BUY 10 T @10 + 1 fee = 101 new money
    #  d1 DIV 10 T x 0.5 = 5 cash
    #  d2 SELL 10 T @12 = 120 cash  -> cash 125
    #  d2 BUY 20 U @8 = 160, from_cash all (125) -> 35 new money
    #  money put in = 101 + 35 = 136; U closes 8.5 on d3 -> value 170; outcome 34
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    prices = {"ticker": "T", "cols": [], "rows": mkrows([(d, c) for d, c in zip(days, [10.0, 11.0, 12.0, 12.5])])}
    u_rows = mkrows([("2026-01-06", 8.0), ("2026-01-07", 8.5)])
    frames = [{"ticker": "U", "rows": u_rows}]
    ledger = [
        {"date": days[0], "type": "BUY", "ticker": "T", "units": 10, "price": 10.0, "fees": 1.0, "provisional": False},
        {"date": days[1], "type": "DIV", "ticker": "T", "units": 10, "dps": 0.5, "amount": None, "provisional": False},
        {"date": days[2], "type": "SELL", "ticker": "T", "units": 10, "price": 12.0, "fees": 0, "provisional": False},
        {"date": days[2], "type": "BUY", "ticker": "U", "units": 20, "price": 8.0, "fees": 0,
         "from_cash": "all", "provisional": False},
    ]
    p, _ = engine.build(prices, ledger, frames=frames)
    m, b = p["money"], p["events"]["buys"][-1]
    check("switch: the switch buy takes all cash (sale 120 + dividend 5)",
          b["from_cash"] == 125.0 and b["new_money"] == 35.0, f"{b}")
    check("switch: money put in = first buy + new money only (101 + 35)", m["invested"] == 136.0, f"{m}")
    check("switch: value from the NEW fund's own raw close (20 x 8.5)",
          p["position"]["value"] == 170.0 and [h["ticker"] for h in p["position"]["holdings"]] == ["U"])
    check("switch: cash ends at 0, outcome = 170 + 0 - 136 = 34",
          m["cash"] == 0.0 and m["total_outcome"] == 34.0, f"{m}")
    check("switch: dividends still counted once (5), market gain = outcome - dividends",
          m["dividends_received"] == 5.0 and m["market_gain"] == 29.0, f"{m}")
    check("switch: outcome series = holdings + cash on each day (incl. the switch day)",
          p["series"]["outcome"] == [100.0, 115.0, 160.0, 170.0], f'{p["series"]["outcome"]}')
    check("switch: money-in line steps only by new money",
          p["series"]["invested"] == [101.0, 101.0, 136.0, 136.0], f'{p["series"]["invested"]}')
    # a fund that does not trade one day keeps its last close (carry forward)
    gap, _ = engine.build({"ticker": "T", "cols": [], "rows": prices["rows"] + mkrows([("2026-01-08", 13.0)])},
                       ledger, frames=frames)
    check("switch: a day without a trade in U values it at its last close",
          gap["position"]["holdings"][0]["last_close"] == 8.5 and
          gap["position"]["holdings"][0]["last_price_date"] == "2026-01-07")
    # loud failures
    greedy = ledger[:3] + [dict(ledger[3], from_cash=500)]
    try:
        engine.build(prices, greedy, frames=frames)
        check("switch: taking more cash than exists is refused", False)
    except engine.EngineError:
        check("switch: taking more cash than exists is refused", True)
    try:
        engine.build(prices, ledger)  # no price frame for U
        check("switch: a fund with no prices is refused", False)
    except engine.EngineError:
        check("switch: a fund with no prices is refused", True)
    # guard: each fill checked against ITS OWN fund's day range
    lk = {r[0]: r for r in prices["rows"]}
    fl = {"U": {r[0]: r for r in u_rows}}
    check("guard: switch ledger passes", guard.validate_ledger(ledger, lk, fl, "T") == [],
          str(guard.validate_ledger(ledger, lk, fl, "T")))
    wrong = ledger[:3] + [dict(ledger[3], price=12.0)]  # 12 is inside T's range, outside U's
    check("guard: a fill outside its own fund's range fails (not checked against another fund)",
          any("U" in e for e in guard.validate_ledger(wrong, lk, fl, "T")))
    check("guard: a fund with no price data fails clearly",
          any("no price data for U" in e for e in guard.validate_ledger(ledger, lk, {}, "T")))
    over = ledger + [{"date": days[3], "type": "SELL", "ticker": "T", "units": 1, "price": 12.5,
                      "fees": 0, "provisional": False}]
    check("guard: units tracked per fund (selling T after it is gone fails)",
          any("units of T" in e for e in guard.validate_ledger(over, lk, fl, "T")))


def test_real_data_smoke():
    import json
    root = Path(__file__).resolve().parents[1]
    pf, tf = root / "data" / "prices.json", root / "data" / "transactions.json"
    if not (pf.exists() and tf.exists()):
        print("  skip real-data smoke (no data files)")
        return
    prices = json.loads(pf.read_text(encoding="utf-8"))
    ledger = json.loads(tf.read_text(encoding="utf-8"))["transactions"]
    check("real: price frame passes guard",
          guard.validate_price_rows(prices["rows"]) == [])
    lookup = {r[0]: r for r in prices["rows"]}
    ppf = root / "data" / "price_patches.json"
    if ppf.exists():
        pdates = [x["date"] for x in json.loads(ppf.read_text(encoding="utf-8"))["rows"]]
        check("real: every patched session is in the price index",
              all(d in lookup for d in pdates), str([d for d in pdates if d not in lookup]))
    frames = [json.loads(f.read_text(encoding="utf-8"))
              for f in sorted((root / "data" / "watch").glob("*.json"))]
    fl = {f["ticker"]: {r[0]: r for r in f["rows"]} for f in frames if f.get("rows")}
    lerrs = guard.validate_ledger(ledger, lookup, fl, prices["ticker"])
    check("real: ledger passes guard (each fill inside its own fund's day range)", lerrs == [], str(lerrs[:3]))
    p, edu = engine.build(prices, ledger, watch=frames, frames=frames)
    hs = p["position"]["holdings"]
    check("real: value = sum over funds of units x RAW last close",
          abs(p["position"]["value"] - sum(h["units"] * h["last_close"] for h in hs)) < 0.01 and
          all(abs(h["value"] - h["units"] * h["last_close"]) < 0.01 for h in hs))
    m = p["money"]
    check("real: outcome = holdings + cash - money put in (double-count guard)",
          abs(m["total_outcome"] - (p["position"]["value"] + m["cash"] - m["invested"])) < 0.02)
    expected_prov = any(bool(t.get("provisional")) for t in ledger)
    check("real: provisional flag matches ledger rows", p["provisional"] is expected_prov)
    check("real: education history starts 2008 (Yahoo depth)",
          edu["history_start"].startswith("2008"))
    tr = p["trend"]
    check("real: trend close_now == main fund's last RAW close",
          tr["close_now"] == prices["rows"][-1][4])
    check("real: trend sma is 200-trading-day and defined (deep history)",
          tr["sma_window"] == 200 and tr["sma_now"] is not None)
    check("real: trend above/below matches close vs sma",
          tr["above"] == (tr["close_now"] >= tr["sma_now"]))
    wf = root / "data" / "watch" / "GAB.SI.json"
    if wf.exists():
        wd = json.loads(wf.read_text(encoding="utf-8"))
        check("real: watched GAB.SI frame passes the row guard",
              guard.validate_price_rows(wd["rows"]) == [], str(guard.validate_price_rows(wd["rows"])[:3]))
    fz = p["projection"]
    check("real: funnel starts from the first row of history (conservative start)",
          fz["history_start"] == prices["rows"][0][0] == edu["history_start"])
    check("real: default growth is the signed-off 7% ASSUMPTION, labelled as such",
          abs(fz["cagr"] - engine.PROJECTION_ASSUMED_CAGR) < 1e-6 and fz["growth_basis"] == "assumed"
          and fz["assumed_range"] == [0.06, 0.08], f'{fz["cagr"]} {fz["growth_basis"]}')
    check("real: history growth (shown beside it) == since-start total-return CAGR",
          abs(fz["hist_cagr"] - edu["long_run"]["cagr_since_start"]) < 1e-5,
          f'{fz["hist_cagr"]} vs {edu["long_run"]["cagr_since_start"]}')
    sm10 = edu["series_monthly"]
    ten = sorted((sm10[i + 120][1] / sm10[i][1]) ** 0.1 - 1 for i in range(len(sm10) - 120))
    r10 = fz["rolling10"]
    check("real: 10-year-stretch figures match an independent count from monthly history",
          r10 is not None and r10["count"] == len(ten) and abs(r10["median"] - ten[len(ten) // 2]) < 2e-3
          and r10["p10"] <= r10["median"] <= r10["p90"], f"{r10} vs median {ten[len(ten) // 2]:.4f}")
    check("real: funnel default horizon 10y, offers up to 20y",
          fz["default_years"] == 10 and fz["years"] == 20)
    # "Show a real past 10 years" replays education.series_monthly in the page
    sm = edu["series_monthly"]
    check("real: monthly history is one point per month, in order (real-stretch replay)",
          all(sm[i][0][:7] < sm[i + 1][0][:7] for i in range(len(sm) - 1)) and
          all(v > 0 for _, v in sm))
    check("real: enough monthly history to replay at least one 10-year stretch",
          len(sm) >= 121, f"{len(sm)} points")
    # Model check: the lognormal 10-year spread must agree with a 12-month block
    # bootstrap of REAL monthly returns (demeaned, so only dispersion is compared).
    import random
    rows = prices["rows"]
    me = [r[5] for i, r in enumerate(rows) if i + 1 == len(rows) or rows[i + 1][0][:7] != r[0][:7]]
    lr = [math.log(me[i] / me[i - 1]) for i in range(1, len(me))]
    mbar = sum(lr) / len(lr)
    rnd = random.Random(20261012)
    outs = []
    for _ in range(3000):
        tot, k = 0.0, 0
        while k < 120:
            st = rnd.randrange(len(lr) - 12)
            for j in range(12):
                if k < 120:
                    tot += lr[st + j] - mbar
                    k += 1
        outs.append(tot)
    outs.sort()
    boot = outs[int(0.9 * len(outs))] - outs[int(0.1 * len(outs))]
    model = 2 * 1.2815516 * fz["sigma"] * math.sqrt(10)
    check("real: funnel 10y P10-P90 spread within 25% of real-history bootstrap",
          abs(model / boot - 1) < 0.25, f"model {model:.3f} vs bootstrap {boot:.3f}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"all green — {len(tests)} test groups")


if __name__ == "__main__":
    main()
