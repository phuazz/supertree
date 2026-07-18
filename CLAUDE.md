# CLAUDE.md — supertree project rules

Layers on top of the vault `C:\dev\CLAUDE.md`. Where they conflict, this file wins.

## Identity and hosting — binding

- Public repo, **unlisted**: the page carries `<meta name="robots" content="noindex">` and this project is **never added to the phuazz.github.io hub** (no `DASH[]` entry, no count bump).
- The page name is **"Phua JR"** — surname only. No first name, no age, no school, no account or broker identifiers anywhere in this repo, ever. The repo description stays generic ("personal family portfolio tracker").
- Full project context (whose portfolio, the kickoff interview) lives only in `KICKOFF_supertree.md` in private vault-docs — do not restate it here or in README.

## Engine rules — binding

- **Valuation = units × raw close. Adjusted close never enters the valuation path.** Dividends are separate cash rows in the ledger (assumption A2: received as cash, not reinvested). Total outcome = value + Σ dividends − money in. This is the double-count guard; `scripts/tests.py` asserts it.
- `data/transactions.json` is the single source of truth. Drafted rows carry `provisional: true`; provisional status must propagate to every derived figure and render visibly on the page. The inception row is a placeholder (2025-06-30) until ZH supplies the real date and cost.
- All ledger dates must be actual SGX trading dates (validated against the price index).
- Long-run return figures, projection bands and "biggest fall" episodes are computed from fetched data, never asserted from memory. Projection bands require ZH sign-off before they render on the page.

## Operations — binding

- **Guard before cron.** The Actions refresh must run `scripts/tests.py` first and use the keep-last-good replacement guard in `scripts/fetch.py`. If the guard cannot run, the refresh does not run.
- The pipeline must fail the build if the built `docs/index.html` is missing the `noindex` meta tag.
- No coupling to PCC or any private repo — no imports, no data flows, in either direction.

## Dates

- Python `datetime` months are 1-indexed — state this in comments at date-construction sites. In the template, JavaScript `Date` months are 0-indexed — same rule.
- ISO `YYYY-MM-DD` strings end to end; the price index is the trading calendar.
- Month-boundary and year-boundary tests in `scripts/tests.py` are mandatory and must keep passing.

## Style

- Tokens verbatim from `C:\dev\design.md` (2026-07-17 vintage or later): light theme, `--X` for fills / `--X-text` for text, monospaced numerals, all figures ≥1,000 comma-grouped, SVG text sized from measured scale. The front page targets an age-11 reading level — short sentences, no jargon without a plain-language gloss.
