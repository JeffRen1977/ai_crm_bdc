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
                   |  extract_portfolio.py |      (future)
                   +-----------+-----------+
                               |
                               v
                      extracted/<cik>/facts/
                               |
         +---------------------+---------------------+
         |                     |                     |
         v                     v                     v
  compute_risk.py       build_investor_       build_portfolio_
     ✅ v0              report.py             view.py
         |                     |                     |
         v                     v                     v
  reports/YYYY-MM-DD/   reports/investors/   reports/portfolio/
     risk_*.json           email bodies         aggregated views
     alert_*.json
         |                                           |
         +-----------+---------------+---------------+
                     v               v
               send_reports.py   notifications.yaml
```

## Module contracts

### 1. Portfolio Management Module

**Goal.** Maintain, at any point in time, a consolidated view of the
loans every BDC in the universe holds — borrower, coupon, floor,
floor type, spread, industry, maturity, cost, fair value,
non-accrual status, PIK toggle.

**Inputs.** 10-Q / 10-K Consolidated Schedule of Investments
(parsed by `extract_portfolio.py`, planned).

**Outputs.**
- `extracted/<cik>/<accession>/portfolio.json` — per-filing loan rows.
- `reports/portfolio/<DATE>/industry_exposure.json` — aggregate by
  GIC/SIC, scoped to a client's BDC sleeve.
- `reports/portfolio/<DATE>/borrower_overlap.json` — borrowers that
  appear in multiple BDCs (contagion flag).

### 2. Investor Reporting Module

**Goal.** Generate client-ready periodic reports with consistent KPIs
and an audit trail linking every number back to the source filing.

**Inputs.** XBRL Company Facts (NAV/share, NII, distributions), plus
outputs of Portfolio Management.

**Outputs.**
- `reports/investors/<DATE>/<client>.html` — HTML with tables +
  per-metric citations.
- `reports/investors/<DATE>/<client>.pdf` — optional, via
  `weasyprint` (future).
- Delivered via `scripts/send_reports.py` using the routing in
  `ingest/notifications.yaml`.

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

Composite score → band: `low` (0–24), `medium` (25–49), `high` (50–74),
`critical` (75–100). Non-accrual % and industry HHI land when
`extract_portfolio.py` ships (Schedule of Investments parsing).

**Alert rules** fire independently of the composite. Currently shipped:
`leverage_breach` (critical), `leverage_elevated` (high),
`dividend_coverage_shortfall` (high), `nav_decline` (medium),
`pik_overreliance` (medium), `unrealized_loss` (medium). Rules in the
same `group` dedupe to the highest severity that fired.

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
- **Risk engine** — `compute_risk.py` + `ingest/risk_weights.yaml`.
  Reads `extracted/<cik>/facts/summary.json`, emits per-BDC scorecards,
  a universe roll-up, and one `alert_*.json` per firing rule. See
  [`RISK_MODEL.md`](RISK_MODEL.md).
- **Risk alert dispatcher** — `send_risk_alerts.py` +
  `send-risk-alerts.sh`. Routes `alert_RISK-*.json` to email via the
  recipients / severity filter in `ingest/notifications.yaml`.
  Supports per-alert or `--digest` mode, dry-run, and is idempotent
  via `reports/<DATE>/.sent/`. The daily orchestrator invokes it
  with `--send-alerts`.

Still to build: `extract_portfolio.py` (Schedule of Investments),
`build_investor_report.py`, `send_reports.py`. See the top-level
[`README.md`](../README.md) for current runnable CLIs.

## Disclaimer

The risk score is an **internal heuristic**. It does not constitute
investment advice, a recommendation, or a prediction of returns.
Every output file and every emailed report carries this disclaimer.
