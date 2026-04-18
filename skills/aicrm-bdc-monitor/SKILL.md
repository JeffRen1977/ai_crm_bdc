---
name: aicrm-bdc-monitor
description: Daily pipeline that turns SEC EDGAR BDC filings into portfolio, investor, and risk outputs for the PriCredit AI-CRM agent. Invoke when a user asks about BDC risk, a specific fund's filings, or portfolio-wide credit signals.
---

# Skill: `aicrm-bdc-monitor`

## Contract (today, v0)

The ingestion spine + XBRL parsing + risk scoring are all live. When
invoked this skill:

1. Ensures `PRICREDIT_UA_EMAIL` is set (EDGAR compliance).
2. Refreshes `bdc/bdc_universe.json` if older than 7 days via
   [`scripts/discover_bdcs.py`](../../scripts/discover_bdcs.py).
3. Pulls recent 10-K / 10-Q / 8-K for the universe via
   [`scripts/fetch_filings.py`](../../scripts/fetch_filings.py).
4. Parses each BDC's XBRL company facts into canonical
   NAV / NII / leverage / asset coverage / PIK / FV-to-cost time
   series via
   [`scripts/parse_filings.py`](../../scripts/parse_filings.py),
   with 10-K/A restatements auto-superseding originals.
5. Extracts Schedule-of-Investments aggregates (industry HHI,
   affiliation split, direct non-accrual % where tagged) via
   [`scripts/extract_portfolio.py`](../../scripts/extract_portfolio.py)
   and merges them into the same per-BDC `summary.json`.
6. Scores each BDC with
   [`scripts/compute_risk.py`](../../scripts/compute_risk.py) using
   the weights/curves/alert rules in
   [`ingest/risk_weights.yaml`](../../ingest/risk_weights.yaml) and
   emits `risk_<ticker>.json`, `risk_summary.json`, and one
   `alert_RISK-<ticker>-*.json` per firing rule (including the new
   `non_accrual_elevated` and `industry_concentration` rules).
7. Builds per-BDC investor briefs (markdown + JSON) and a universe
   `index.md` / `index.json` via
   [`scripts/build_investor_report.py`](../../scripts/build_investor_report.py),
   composing `summary.json` + `portfolio.json` + `risk_*.json` +
   `alert_*.json` — no additional EDGAR calls. Output lands in
   `reports/<DATE>/briefs/`.
8. Dispatches alerts to routes configured in
   [`ingest/notifications.yaml`](../../ingest/notifications.yaml)
   via [`scripts/send_risk_alerts.py`](../../scripts/send_risk_alerts.py)
   with idempotency markers in `reports/<DATE>/.sent/`.

All steps are wrapped by
[`scripts/run-daily-pricredit.sh`](../../scripts/run-daily-pricredit.sh)
(flags `--skip-parse` / `--skip-portfolio` / `--skip-risk` /
`--skip-reports` / `--send-alerts` / `--digest`).

## Contract (planned, v1)

9. HTML-table-based Schedule of Investments parser in
   `extract_portfolio.py` so non-accrual coverage jumps from the
   current 2/52 direct-tag BDCs to ~90%.
10. SMTP / HTML / PDF delivery of the investor briefs (reusing the
    dispatcher in `send_risk_alerts.py`) via `send_reports.py`.

## Output contract

- Every number carries a citation object:
  `{"cik": "...", "accession": "...", "form": "10-Q",
    "concept": "us-gaap:NetAssetValuePerShare",
    "as_of": "2025-12-31"}`.
- Every report starts with an "informational, not investment advice"
  disclaimer and identifies the model version.

## Directory conventions

```
PriCredit/
├── bdc/bdc_universe.json              # {cik,name,ticker,...}[]
├── filings/<cik>/<accession>/
│   ├── meta.json                      # our bookkeeping
│   ├── index.json                     # EDGAR filing index
│   └── <primary_document>             # 10-K/10-Q/8-K HTML/PDF
├── extracted/<cik>/facts/             # XBRL canonical facts
│   ├── timeseries.json                # full history per metric
│   ├── latest.json                    # latest observation per metric
│   ├── resolved.json                  # XBRL tag -> canonical audit trail
│   └── summary.json                   # one-page snapshot (latest + derived + nav_trend + portfolio)
├── extracted/<cik>/portfolio/<accession>/
│   ├── portfolio.json                 # industry HHI, affiliation split, non-accrual %
│   └── source.json                    # filing provenance for the aggregates
├── reports/<DATE>/pricredit.log       # operator log
├── reports/<DATE>/parse_summary.json  # XBRL coverage roll-up
├── reports/<DATE>/portfolio_summary.json  # SoI extraction coverage roll-up
├── reports/<DATE>/risk_<ticker>.json  # per-BDC scorecard
├── reports/<DATE>/risk_summary.json   # universe roll-up
├── reports/<DATE>/alert_RISK-*.json   # one file per firing rule
└── reports/<DATE>/briefs/             # investor memos (markdown + JSON)
    ├── <TICKER>.md                    # human-readable per-BDC brief
    ├── <TICKER>.json                  # pricredit.investor_brief/v0
    ├── index.md                       # universe roll-up, sorted by score
    └── index.json                     # pricredit.investor_brief_index/v0
```

## Failure modes to watch

- **EDGAR 403** → UA was rejected; the client raises `EdgarConfigError`.
  Check that `PRICREDIT_UA_EMAIL` is a real address you control.
- **EDGAR 429 / 503** → rate limiter retries with jitter; if it still
  fails, pause the pipeline and retry later.
- **Missing XBRL tag** → not every BDC tags everything. Fall back to
  text parsing, never fabricate a number.
- **N-54A without a ticker** → the entity isn't publicly traded; keep
  in universe with `publicly_traded: false` but don't ship it in
  investor reports.
