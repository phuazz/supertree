"""supertree pipeline — engine outputs + inject into template -> docs/index.html

Usage: python scripts/pipeline.py [--refresh]
--refresh runs fetch.py first. A fetch guard failure (exit 2) keeps last-good
data and the build CONTINUES, so the stale/fail badge ships — a dead feed must
be visible on the page, not silent. The build itself FAILS on ledger or engine
errors, and refuses to ship if the noindex meta is missing from the output.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import engine
import guard

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DOCS = ROOT / "docs"
MARKER = "window.__DATA__ = null;"


def read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_json(p, obj):
    Path(p).write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")


def main():
    if "--refresh" in sys.argv:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch.py")])
        if r.returncode not in (0, 2):  # 2 = guard kept last-good; still build
            sys.exit(f"fetch crashed with exit {r.returncode}")

    prices = read_json(DATA / "prices.json")
    status = read_json(DATA / "status.json")
    ledger = read_json(DATA / "transactions.json")["transactions"]

    lookup = {row[0]: row for row in prices["rows"]}
    errs = guard.validate_ledger(ledger, lookup)
    if errs:
        sys.exit("LEDGER GUARD FAIL:\n  " + "\n  ".join(errs))

    portfolio, education = engine.build(prices, ledger)
    write_json(DATA / "portfolio.json", portfolio)
    write_json(DATA / "education.json", education)

    template = (ROOT / "template.html").read_text(encoding="utf-8")
    if MARKER not in template:
        sys.exit("template marker missing — cannot inject data")
    payload = json.dumps({"portfolio": portfolio, "education": education,
                          "status": status}, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")  # never close the script tag early
    built = template.replace(MARKER, "window.__DATA__ = " + payload + ";", 1)
    if 'name="robots" content="noindex"' not in built:
        sys.exit("noindex meta missing from build — refusing to ship (project rule)")

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(built, encoding="utf-8")
    nojekyll = DOCS / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")

    m = portfolio["money"]
    print(f"built docs/index.html ({len(built.encode('utf-8')):,} bytes)")
    print(f"as_of {portfolio['as_of']}  value S${portfolio['position']['value']:,}  "
          f"outcome S${m['total_outcome']:,} ({m['total_outcome_pct']:.1%})  "
          f"provisional={portfolio['provisional']}")


if __name__ == "__main__":
    main()
