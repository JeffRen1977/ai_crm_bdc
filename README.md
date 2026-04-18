# PriCredit — Investor AI-CRM

Independent OpenClaw-style agent that turns **SEC EDGAR** disclosures from
**Business Development Companies (BDCs)** into portfolio-analytics,
investor-facing reports, and forward-looking risk signals.

## Module map

| Module | Purpose | Primary inputs |
|--------|---------|----------------|
| **Portfolio Management** | Aggregate BDC holdings, loan-level exposure (non-accrual, PIK, industry, maturity). | 10-Q / 10-K *Schedule of Investments* tables. |
| **Investor Reporting** | Periodic client-ready summaries: NAV trend, dividend coverage, credit quality. | XBRL Company Facts + 10-Q/10-K narrative. |
| **Risk Management Engine** | Per-BDC and portfolio-level risk score (leverage, non-accrual %, PIK %, NAV trend, industry concentration). | All of the above + macro overlays. |

Today this repository ships the **ingestion foundation** only — EDGAR
client, BDC universe discovery, and filings downloader. The three modules
above land in follow-up commits once the raw data pipeline is trusted.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt

# EDGAR requires a real contact in the User-Agent. Export once:
echo 'export PRICREDIT_UA_EMAIL=jianfengren.sd@gmail.com' >> ~/.pricredit-env
source ~/.pricredit-env

# 1) Build the BDC universe (tickers + CIKs, cached locally).
scripts/discover_bdcs.py --out bdc/bdc_universe.json

# 2) Pull recent 10-K / 10-Q / 8-K for the universe.
scripts/fetch_filings.py --universe bdc/bdc_universe.json \
                         --forms 10-K,10-Q,8-K --limit-per-form 4

# 3) Daily orchestrator (discovers BDCs if missing, then fetches filings).
scripts/run-daily-pricredit.sh
```

## Directory layout

```
PriCredit/
├── AGENTS.md / SOUL.md           identity + contract with OpenClaw peers
├── docs/                          EDGAR usage, BDC primer, architecture
├── skills/aicrm-bdc-monitor/      daily pipeline skill (contract + TODOs)
├── bdc/bdc_universe.json          ticker + CIK registry (generated)
├── filings/<cik>/<accession>/     raw 10-K/10-Q/8-K (git-ignored)
├── extracted/<cik>/<accession>/   parsed structured output (git-ignored)
├── reports/<DATE>/                daily risk digests (git-ignored)
├── ingest/notifications.yaml      report recipient routing
└── scripts/                       EDGAR client + CLI utilities
```

## Safety & compliance

- EDGAR **requires** a compliant `User-Agent: Name email` and enforces a
  10 req/sec rate cap. All HTTP goes through `scripts/_edgar_common.py`
  which pins both. See [`docs/EDGAR_USAGE.md`](docs/EDGAR_USAGE.md).
- Downloaded filings and extracted parses are **not** committed — they're
  easily regenerated from EDGAR and would bloat the repo.
- Risk outputs are **informational only**, not investment advice. See
  the disclaimer in every generated report.
