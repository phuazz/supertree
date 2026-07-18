"""supertree test suite — zero-dependency, run with: python scripts/tests.py

Covers the four declared silent-failure modes: bad fetch overwriting good data,
dividend double-count, date/boundary drift, provisional flags not propagating.
The Actions refresh runs this FIRST and stops on failure (guard before cron).
"""
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


# ------------------------------------------------------------ real data ----
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
    check("real: ledger passes guard", guard.validate_ledger(ledger, lookup) == [],
          str(guard.validate_ledger(ledger, lookup)[:3]))
    p, edu = engine.build(prices, ledger)
    check("real: value = units x last close",
          abs(p["position"]["value"] -
              p["position"]["units"] * p["position"]["last_close"]) < 1e-6)
    check("real: provisional propagates (all rows drafted)", p["provisional"] is True)
    check("real: education history starts 2008 (Yahoo depth)",
          edu["history_start"].startswith("2008"))


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
