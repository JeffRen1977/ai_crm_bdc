# PriCredit architecture

Three modules share one ingestion spine.

```
                   +-------------------------+
                   |  EDGAR (data.sec.gov,   |
                   |  www.sec.gov/Archives)  |
                   +-----------+-------------+
                               |
                               v
                  scripts/_edgar_common.py
                  (throttled, UA-compliant, cached)
                               |
             +-----------------+-----------------+
             |                                   |
             v                                   v
   scripts/discover_bdcs.py            scripts/fetch_filings.py
   (N-54A -> universe)                 (10-K/10-Q/8-K downloads)
             |                                   |
             v                                   v
     bdc/bdc_universe.json               filings/<cik>/<accession>/
             |                                   |
             +-----------------+-----------------+
                               |
                               v
                   +-----------+-----------+
                   |   parse_filings.py    |      ✅ v0
                   |  extract_portfolio.py |      ✅ v0
                   +-----------+-----------+
                               |
                               v
                      extracted/<cik>/facts/
                               |
         +---------------------+---------------------+
         |                     |                     |
         v                     v                     v
  compute_risk.py       build_investor_       build_portfolio_
     ✅ v0              report.py  ✅ v0     view.py
         |                     |                     |
         v                     v                     v
  reports/YYYY-MM-DD/   reports/YYYY-MM-DD/   reports/portfolio/
     risk_*.json           briefs/             aggregated views
     alert_*.json          <TICKER>.md + .json
                           index.md + .json
         |                     |                     |
         +-----------+---------+---------------------+
                     v                     v
               send_risk_alerts.py   notifications.yaml
```

## Module contracts

### 1. Portfolio Management Module

**Goal.** Maintain, at any point in time, a consolidated view of the
loans every BDC in the universe holds — borrower, coupon, floor,
floor type, spread, industry, maturity, cost, fair value,
non-accrual status, PIK toggle.

**Inputs.** 10-Q / 10-K Consolidated Schedule of Investments, parsed
as inline XBRL by `extract_portfolio.py` (see
[`PORTFOLIO_MODEL.md`](PORTFOLIO_MODEL.md)).

**Outputs.**
- `extracted/<cik>/portfolio/<accession>/portfolio.json` — total FV,
  industry HHI, top industries, affiliation split, directly-tagged
  non-accrual % (currently ARCC + CION; remainder null until a v1
  HTML-table SoI parser lands).
- `extracted/<cik>/portfolio/<accession>/source.json` — filing
  provenance.
- `extracted/<cik>/facts/summary.json` — v0 also merges a compact
  `portfolio:` block here so `compute_risk.py` consumes the new
  signals without a second fetch.
- `reports/<DATE>/portfolio_summary.json` — per-run coverage roll-up.

Planned (v1): per-loan `portfolio.json` with issuer + coupon +
maturity + PIK flag, plus cross-BDC `borrower_overlap.json`.

### 2. Investor Reporting Module

**Goal.** Generate client-ready per-BDC memos with consistent KPIs
and an audit trail linking every number back to the source filing.

**Inputs.** `extracted/<cik>/facts/{summary,timeseries}.json`,
`extracted/<cik>/portfolio/<accn>/portfolio.json`,
`reports/<DATE>/risk_<ticker>.json`, and
`reports/<DATE>/alert_RISK-<ticker>-*.json`. All artifacts are
already on disk — the brief generator never re-hits EDGAR.

**Outputs (v0, `pricredit.investor_brief/v0`).** Everything lands in
`reports/<DATE>/briefs/`:

- `<TICKER>.md` — human-readable investor memo: headline + band,
  risk snapshot with top contributors, open alerts with triggers,
  15-row financial snapshot, NAV trend (QoQ/YoY + 8-quarter
  sparkline + table), portfolio snapshot (HHI + effective-N + top 5
  industries + affiliation mix + non-accrual %), factor audit,
  methodology footer.
- `<TICKER>.json` — machine-readable mirror (same facts, stable
  schema).
- `index.md` / `index.json` — universe roll-up sorted by composite
  score with band counts and per-BDC headline numbers.

See [`INVESTOR_REPORT.md`](INVESTOR_REPORT.md) for the full schema
and rendering contract.

Planned: HTML / PDF rendering and an SMTP delivery path
(`send_reports.py`, reusing the dispatcher in
`send_risk_alerts.py`).

### 3. Risk Management Engine

**Goal.** A per-BDC and per-client-sleeve risk score that's
transparent, documented, and easy to recompute.

**Inputs.** Outputs from Portfolio Management + Investor Reporting.

**v1 score (heuristic, piecewise-linear).** Each factor converts one
raw metric to a 0-100 sub-score via threshold curves declared in
`ingest/risk_weights.yaml`; composite is the weight-renormalized
average of factors we had data for. Missing factors are excluded
rather than penalized. Factors shipped in v0:

| Factor              | Input                                | Weight |
|---------------------|--------------------------------------|--------|
| `asset_coverage`    | `latest.asset_coverage_ratio` (fallback: `derived.leverage_debt_to_equity`) | 2.0 |
| `fair_value_to_cost`| `derived.fair_value_to_cost`         | 1.0    |
| `pik_income_ratio`  | `derived.pik_income_ratio`           | 1.0    |
| `dividend_coverage` | `derived.dividend_coverage_nii_over_divs` | 1.5 |
| `nav_yoy`           | `nav_trend.yoy.pct_change`           | 1.5    |
| `nav_qoq`           | `nav_trend.qoq.pct_change`           | 1.0    |
| `industry_hhi`      | `portfolio.industry_hhi`             | 1.0    |
| `non_accrual_fv`    | `portfolio.non_accrual_pct_fair_value` | 1.5  |

Composite score → band: `low` (0–24), `medium` (25–49), `high` (50–74),
`critical` (75–100).

**Alert rules** fire independently of the composite. Currently shipped:
`leverage_breach` (critical), `leverage_elevated` (high),
`dividend_coverage_shortfall` (high), `non_accrual_elevated` (high),
`nav_decline` (medium), `pik_overreliance` (medium),
`unrealized_loss` (medium), `industry_concentration` (medium). Rules in
the same `group` dedupe to the highest severity that fired.

Full methodology + curves + rationale:
[`RISK_MODEL.md`](RISK_MODEL.md).

**Outputs.**
- `reports/<DATE>/risk_<ticker>.json` — per-BDC scorecard with full
  factor audit (raw value, curve, weight share, contribution).
- `reports/<DATE>/risk_summary.json` — universe roll-up sorted by score.
- `reports/<DATE>/alert_RISK-<ticker>-<YYYYMMDD>-NNN.json` — one file
  per firing alert rule. Shape is compatible with the idvault alert
  format so the same email dispatcher can consume it.

## What ships today

- **Ingestion spine** — `_edgar_common.py`, `discover_bdcs.py`,
  `fetch_filings.py`, `run-daily-pricredit.sh` (top grey boxes).
- **XBRL parsing** — `parse_filings.py` + `_xbrl_concepts.py`.
  Produces per-BDC canonical time series under
  `extracted/<cik>/facts/{timeseries,latest,resolved,summary}.json`,
  with 10-K/A restatements auto-superseding originals. See
  [`XBRL_CONCEPT_MAP.md`](XBRL_CONCEPT_MAP.md).
- **Schedule of Investments parsing** — `extract_portfolio.py` +
  `_soi_parser.py`. Parses inline XBRL in the primary 10-K / 10-Q and
  emits `portfolio.json` (industry HHI, affiliation split, direct
  non-accrual %) for 52/52 BDCs, industry HHI for 46/52. See
  [`PORTFOLIO_MODEL.md`](PORTFOLIO_MODEL.md).
- **Risk engine** — `compute_risk.py` + `ingest/risk_weights.yaml`.
  Reads `extracted/<cik>/facts/summary.json` (inc. the `portfolio:`
  block), emits per-BDC scorecards, a universe roll-up, and one
  `alert_*.json` per firing rule. See [`RISK_MODEL.md`](RISK_MODEL.md).
- **Risk alert dispatcher** — `send_risk_alerts.py` +
  `send-risk-alerts.sh`. Routes `alert_RISK-*.json` to email via the
  recipients / severity filter in `ingest/notifications.yaml`.
  Supports per-alert or `--digest` mode, dry-run, and is idempotent
  via `reports/<DATE>/.sent/`. The daily orchestrator invokes it
  with `--send-alerts`.
- **Investor brief generator** — `build_investor_report.py`. Composes
  `summary.json` + `portfolio.json` + `risk_*.json` + `alert_*.json`
  into per-BDC markdown + JSON briefs and a universe `index.md` /
  `index.json`, all under `reports/<DATE>/briefs/`. 52/52 BDCs
  covered on the latest run; deterministic, offline-safe. Wired
  into the daily orchestrator after the risk step; opt-out via
  `--skip-reports` / `SKIP_REPORTS=1`. See
  [`INVESTOR_REPORT.md`](INVESTOR_REPORT.md).

Still to build: `send_reports.py` (email / HTML delivery for the
briefs), and a v1 of `extract_portfolio.py` with HTML-table SoI
parsing so non-accrual coverage lifts from 2/52 BDCs (direct-tag
only) to ~90%. See the top-level [`README.md`](../README.md) for
current runnable CLIs.

## Disclaimer

The risk score is an **internal heuristic**. It does not constitute
investment advice, a recommendation, or a prediction of returns.
Every output file and every emailed report carries this disclaimer.
