# PriCredit Product Design Document (English, Implementation-Aligned)

> Version: v0.1 (implementation-aligned)  
> Updated: 2026-04-21  
> Note: This document is strictly based on the current code and runnable flow, with a clear split between **Now** and **Next**.

## 1. Product Positioning

PriCredit is an AI-CRM data and risk product for **publicly traded BDCs (Business Development Companies)**.  
Its core objective is to build three capability chains from SEC EDGAR disclosures:

1. **Portfolio Management**: extract portfolio structure and concentration signals from 10-K/10-Q Schedule of Investments and XBRL;
2. **Risk Management Engine**: map explainable metrics into factor scores, composite risk scores, and alerts;
3. **Investor Reporting**: compose structured outputs into readable briefs and universe indexes.

Two Beta capabilities are now included:
- **Minimal 8-K event extraction (ARCC-first)**
- **Shadow NAV (experimental, ARCC-first)**

---

## 2. Design Principles

- **Single source of truth: EDGAR**: all upstream data comes from SEC EDGAR (`data.sec.gov` + `www.sec.gov/Archives`).
- **Auditable**: outputs preserve provenance fields whenever possible (form, accession, filed date, source path).
- **Recomputable**: scoring and alert logic are config-driven (`ingest/risk_weights.yaml`) and can be fully rerun after tuning.
- **Offline composition**: reporting consumes on-disk artifacts only; no repeated network fetches.
- **Progressive rollout**: first complete a single-entity vertical slice (ARCC), then scale coverage and model precision.

---

## 3. Users and Typical Scenarios

### 3.1 Target Users
- Credit / liquid credit analysts
- Family office and institutional LP monitoring teams
- Product managers in private credit research and risk functions

### 3.2 Scenarios
- Daily monitoring: identify which BDCs are getting riskier and which thresholds fired;
- Event-driven review: quickly locate 8-K Items and potential credit/valuation implications;
- External communication: generate consistent investor-facing briefs;
- Experimental valuation signal: produce a Shadow NAV early indicator (explicitly marked experimental).

---

## 4. System Architecture (Current)

High-level flow:

1. **BDC universe discovery**: `scripts/discover_bdcs.py`
2. **EDGAR fetch**: `scripts/fetch_filings.py`
3. **Base XBRL parsing**: `scripts/parse_filings.py`
4. **SoI aggregate parsing**: `scripts/extract_portfolio.py`
5. **Risk scoring / alerts**: `scripts/compute_risk.py`
6. **Investor brief generation**: `scripts/build_investor_report.py`
7. **Alert delivery**: `scripts/send_risk_alerts.py` (optional WhatsApp)
8. **8-K event extraction (minimal)**: `scripts/extract_8k_items.py`
9. **Shadow NAV (experimental)**: `scripts/shadow_nav.py`
10. **Orchestration**: `scripts/run-daily-pricredit.sh`

---

## 5. Module Design (Now / Next)

## 5.1 Data Ingestion Layer (EDGAR Ingestion)

**Now**
- Shared EDGAR utilities in `scripts/_edgar_common.py`: rate limiting, retry, UA validation, cache.
- Default forms: `10-K,10-Q,8-K`.
- Optional registration/prospectus forms via `--include-registration` (N-2, 424B2/3/5, 497).
- Output: `filings/<cik>/<accession>/index.json + meta.json + primary_document`.

**Next**
- Structured parsing for registration forms (N-2/497/424B*) including fee terms, investment limits, and risk factors.

## 5.2 Portfolio Management Module

**Now**
- `extract_portfolio.py` parses inline XBRL from 10-Q/10-K and extracts:
  - industry distribution, industry HHI, top industries;
  - affiliate mix;
  - tagged non-accrual % (limited coverage).
- Output: `extracted/<cik>/portfolio/<accession>/portfolio.json`.

**Next**
- HTML table fallback parsing to improve non-accrual coverage;
- finer-grained loan-level fields: debt seniority, coupon/spread, PIK flag, maturity structure.

## 5.3 Risk Management Engine

**Now**
- Driven by `compute_risk.py` + `ingest/risk_weights.yaml`;
- factor metrics mapped to 0–100 using piecewise-linear curves;
- composite score uses weight re-normalization over available factors;
- output bands: `low/medium/high/critical`;
- independent alert rules produce `alert_*.json`.

**Next**
- factor calibration and backtesting;
- improved handling of missing-factor coverage;
- add event-layer factors (8-K signals) as risk inputs.

## 5.4 Investor Reporting Module

**Now**
- `build_investor_report.py` is offline-only composition (no network access);
- Outputs:
  - per-BDC brief: `reports/<DATE>/briefs/<TICKER>.md + .json`
  - universe index: `index.md + index.json`

**Next**
- HTML/PDF rendering;
- distribution automation (`send_reports.py`, planned);
- audience-specific templates (IC version, LP version).

## 5.5 8-K Event Module (Minimal)

**Now**
- `extract_8k_items.py` (ARCC-first):
  - extracts `Item X.XX` references and snippets;
  - flags keyword-based events (e.g., credit facility changes, realized gain/loss).
- Outputs:
  - `extracted/<cik>/events8k/<accession>/events_8k.json`
  - `reports/<DATE>/events8k_summary_<TICKER>.json`

**Next**
- item-level semantic classification and event normalization (amendments, exits/disposals, valuation-relevant events);
- evidence quality scoring and conflict resolution.

## 5.6 Shadow NAV (Experimental)

**Now**
- `shadow_nav.py` uses:
  - latest official NAV from `summary.json` as baseline;
  - conservative heuristic adjustments from post-baseline 8-K events;
- Output: `reports/<DATE>/shadow_nav_<TICKER>.json`;
- Default labeling: `experimental` + `low/medium confidence`.

**Next**
- parameterized mapping from event types to NAV impacts;
- backtest against subsequent official 10-Q/10-K NAV;
- confidence model upgrade (text quality, event intensity, timeliness).

---

## 6. Data and File Contracts (Key)

- `bdc/bdc_universe.json`: base BDC universe (ticker, cik, public status)
- `filings/<cik>/<accession>/meta.json`: fetch metadata and file pointers
- `extracted/<cik>/facts/summary.json`: normalized financial snapshot
- `extracted/<cik>/portfolio/<accession>/portfolio.json`: portfolio aggregate signals
- `extracted/<cik>/events8k/<accession>/events_8k.json`: 8-K event signals (v0)
- `reports/<DATE>/risk_<ticker>.json`: risk scorecard
- `reports/<DATE>/alert_*.json`: rule-based alerts
- `reports/<DATE>/briefs/*.md|*.json`: investor briefs
- `reports/<DATE>/shadow_nav_<ticker>.json`: Shadow NAV (experimental)

---

## 7. End-to-End Daily Run

Recommended entrypoint: `scripts/run-daily-pricredit.sh`

Common run modes:

```bash
# Standard daily run
scripts/run-daily-pricredit.sh

# ARCC deep dive + registration forms + 8-K + Shadow NAV
INCLUDE_REGISTRATION=1 RUN_8K_ITEMS=1 RUN_SHADOW_NAV=1 \
scripts/run-daily-pricredit.sh --tickers ARCC
```

---

## 8. Non-Functional Design

- **Compliance**: enforce valid EDGAR contact UA (`PRICREDIT_UA_EMAIL`);
- **Reliability**: retry + rate limit + cache; single-entity failures do not block full run;
- **Traceability**: run logs and run-summary artifacts;
- **Idempotence**: most steps support rerun and `--force` controls;
- **Security**: credentials in `~/.pricredit-env`, never committed.

---

## 9. Risks and Limitations (Current State)

- Non-accrual coverage is still incomplete (many BDCs do not tag it directly);
- 8-K parsing is currently minimal rule/keyword logic: suitable for triage, not autonomous decisioning;
- Shadow NAV is an experimental signal and cannot replace official NAV;
- full backtesting loop is not yet implemented.

---

## 10. Suggested Next Two Iterations

### Iteration A: 8-K Event Standardization (Priority)
- define event taxonomy (financing, exits, losses, governance, guidance, etc.);
- define whitelist/blacklist rules for “NAV-moving vs narrative”;
- add evidence quality scoring.

### Iteration B: Shadow NAV Calibration
- externalize adjustment parameters (YAML);
- build “predicted vs official subsequent NAV” evaluation reports;
- output error distribution and confidence intervals.

---

## 11. Recommended External Messaging (English)

Use a unified statement:

> PriCredit builds BDC portfolio monitoring, risk scoring, and investor reporting capabilities from SEC EDGAR public disclosures.  
> 8-K event processing and Shadow NAV are currently experimental enhancement modules designed for early risk signal detection and are not investment advice.

