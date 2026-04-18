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
                   |   parse_filings.py    |      (future)
                   |  extract_portfolio.py |
                   +-----------+-----------+
                               |
                               v
                      extracted/<cik>/<accession>/
                               |
         +---------------------+---------------------+
         |                     |                     |
         v                     v                     v
  compute_risk.py       build_investor_       build_portfolio_
                        report.py             view.py
         |                     |                     |
         v                     v                     v
  reports/YYYY-MM-DD/   reports/investors/   reports/portfolio/
     risk_*.json           email bodies         aggregated views
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

**v1 score (heuristic, documented as such):**

```
score =
    w1 * z(leverage_ratio)                  # higher leverage -> worse
  + w2 * z(non_accrual_pct_fair_value)      # higher -> worse
  + w3 * z(pik_income_ratio)                # higher -> worse
  + w4 * z(-nav_per_share_qoq_trend)        # declining NAV -> worse
  + w5 * z(industry_hhi)                    # more concentrated -> worse
  + w6 * z(-dividend_coverage_ratio)        # < 1 coverage -> worse
```

Weights are configurable in `ingest/risk_weights.yaml` (future);
defaults are defensibly equal until we backtest.

**Outputs.**
- `reports/<DATE>/risk_<ticker>.json` — per-BDC detail.
- `reports/<DATE>/risk_summary.json` — portfolio roll-up.
- `reports/<DATE>/risk_alerts.json` — BDCs whose score crossed a
  threshold or whose non-accrual % moved materially.

## What ships today

Only the ingestion spine — the two grey boxes at the top. Everything
else is stubbed in this doc so the direction is legible; follow-up
commits implement each module. See the top-level
[`README.md`](../README.md) for current runnable scripts.

## Disclaimer

The risk score is an **internal heuristic**. It does not constitute
investment advice, a recommendation, or a prediction of returns.
Every output file and every emailed report carries this disclaimer.
