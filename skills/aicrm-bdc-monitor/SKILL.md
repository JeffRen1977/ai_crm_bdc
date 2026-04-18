---
name: aicrm-bdc-monitor
description: Daily pipeline that turns SEC EDGAR BDC filings into portfolio, investor, and risk outputs for the PriCredit AI-CRM agent. Invoke when a user asks about BDC risk, a specific fund's filings, or portfolio-wide credit signals.
---

# Skill: `aicrm-bdc-monitor`

## Contract (today, v0)

Only the ingestion spine is live. When invoked this skill:

1. Ensures `PRICREDIT_UA_EMAIL` is set (EDGAR compliance).
2. Refreshes `bdc/bdc_universe.json` if older than 7 days via
   [`scripts/discover_bdcs.py`](../../scripts/discover_bdcs.py).
3. Pulls recent 10-K / 10-Q / 8-K for the universe via
   [`scripts/fetch_filings.py`](../../scripts/fetch_filings.py).

All three steps are wrapped by
[`scripts/run-daily-pricredit.sh`](../../scripts/run-daily-pricredit.sh).

## Contract (planned, v1)

Once parsing lands, the skill additionally:

4. Parses each filing's financial facts + Schedule of Investments
   into `extracted/<cik>/<accession>/*.json`.
5. Scores per-BDC risk with `compute_risk.py`
   (leverage, non-accrual %, PIK %, NAV trend, industry HHI,
   dividend coverage).
6. Produces investor-ready reports in `reports/<DATE>/investors/`
   and risk alerts in `reports/<DATE>/risk_*.json`.
7. Dispatches to routes configured in
   [`ingest/notifications.yaml`](../../ingest/notifications.yaml).

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
├── bdc/bdc_universe.json           # {cik,name,ticker,...}[]
├── filings/<cik>/<accession>/
│   ├── meta.json                   # our bookkeeping
│   ├── index.json                  # EDGAR filing index
│   └── <primary_document>          # 10-K/10-Q/8-K HTML/PDF
├── extracted/<cik>/<accession>/    # (future) structured parses
├── reports/<DATE>/pricredit.log    # operator log (v0)
└── reports/<DATE>/risk_*.json      # (future) risk outputs
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
