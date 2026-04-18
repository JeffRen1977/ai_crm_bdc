# Portfolio Model (Schedule of Investments)

This document explains how PriCredit turns each BDC's Schedule of
Investments (SoI) — the per-line portfolio disclosure in every 10-K /
10-Q — into the derived signals that feed the risk engine.

## Why a second extractor

`parse_filings.py` pulls XBRL **company facts**, which are
dimension-free time series (NAV, NII, leverage, etc.). It cannot
express anything that varies across portfolio companies or industries,
because those are dimensional facts that EDGAR's `companyfacts` API
does not expose.

The SoI, on the other hand, lives as **inline XBRL (iXBRL)** inside
the filer's primary `.htm` document. Every investment line is tagged
with one or more axis dimensions:

- `us-gaap:EquitySecuritiesByIndustryAxis` — industry / sector
- `us-gaap:InvestmentTypeAxis` — first-lien, second-lien, unitranche,
  preferred equity, etc.
- `us-gaap:InvestmentIdentifierAxis` — issuer / portfolio company
- `us-gaap:InvestmentIssuerAffiliationAxis` — unaffiliated vs
  controlled / non-controlled affiliate

`scripts/extract_portfolio.py` parses these inline facts and emits two
artefacts per BDC:

```
extracted/<cik>/portfolio/<accession>/portfolio.json
extracted/<cik>/portfolio/<accession>/source.json
```

…and merges a compact `portfolio:` block into
`extracted/<cik>/facts/summary.json` so `compute_risk.py` can consume
the new signals without re-reading the 10-K.

## Derived signals (v0)

| Signal | Source | Coverage |
|---|---|---|
| `total_fv` | Sum of FV across affiliation-axis members | 100% |
| `industry_hhi` | Herfindahl on per-industry FV share | 88% (46/52 BDCs) |
| `industry_coverage_pct` | Share of portfolio FV that's industry-tagged | 100% |
| `largest_industry` / `top_industries` | Ranked by FV share | 88% |
| `affiliation_mix` | Share of FV by affiliation member | 100% |
| `non_accrual_pct_fair_value` | Direct tag only (see below) | 4% (2/52) |
| `source_form` / `source_accession` / `period_end` | filing provenance | 100% |

## How the industry decomposition is computed

BDC filers use three different tagging layouts. `extract_portfolio.py`
tries them in priority order, falling through when one yields no facts:

1. **Pure axis** — the filer tags `InvestmentOwnedAtFairValue` at
   contexts whose only dimension is `EquitySecuritiesByIndustryAxis`.
   This is ARCC / GBDC / MFIC / PSEC style. We sum directly.

2. **Multi-axis leaves** — the filer tags each per-investment line at
   a context carrying `{industry + affiliation + type}` together. This
   is OBDC / several newer BDCs. `aggregate_by_axis_best_signature`
   picks the signature under which industry accumulates the largest
   total, which by construction is the leaf level.

3. **Concentration percentage** — MAIN and a few others don't tag
   per-industry FV at all. They publish
   `us-gaap:ConcentrationRiskPercentage1` under
   `(industry_axis, benchmark=InvestmentOwnedAtFairValueMember)`
   contexts; each value is already a share. We use the shares
   directly.

Whichever path fires is recorded as `industry_method` on the output,
so you can triage coverage surprises without re-reading the iXBRL.

The **HHI** is always computed on **shares of tagged investments**,
not of the entire portfolio. `industry_coverage_pct` tells you what
fraction of the portfolio is industry-tagged — when this is low (e.g.,
MFIC 47%, PSEC 28%), the HHI is a directional concentration signal
for the tagged slice, not a universe-complete number. Risk scoring
still uses the raw HHI; a future version may haircut low-coverage
HHIs to dampen noise.

## The non-accrual gap

Only **2 of 52 BDCs** (ARCC, CION) tag non-accrual status as a direct
numeric concept (`arcc:InvestmentOwnedNonAccrualStatusPercentOfFairValue`
and equivalent). Everyone else discloses non-accruals in prose
footnotes keyed to letters in the SoI table ("(a) non-accrual as of
the date of this filing"), which requires an HTML-table parser plus
footnote cross-referencing we haven't built yet.

For now:

- The filing-provided value is used if present (`non_accrual_source:
  "direct_tag"`).
- Otherwise `non_accrual_pct_fair_value` is `null` and the
  `non_accrual_fv` factor is excluded from that BDC's composite score
  (weights renormalize the way any missing factor does).
- The `non_accrual_elevated` alert simply can't fire for a BDC
  without a tagged value — honest over noisy.

v1 of `extract_portfolio.py` should add an HTML SoI parser that reads
the table + footnote legend to recover non-accrual flags. At that
point coverage jumps from 4% to ~90% and the alert becomes the most
credit-meaningful signal in the engine.

## Explicitly deferred to v1

These were in the v0 sketch but dropped to ship a trustworthy first
cut:

- **`loan_type_mix`** (first-lien / second-lien / unitranche / equity
  share). Members carry a parent/child hierarchy (e.g., the filer
  declares both "First Lien" and its "First Lien Unitranche" sub-bucket
  with overlapping totals) and a clean mix needs linkbase parsing to
  avoid double-counting.
- **`top10_issuers_pct`** (single-name concentration). Per-investment
  identifier contexts interleave with industry and type axes and
  would require per-line reconciliation to avoid summing parent +
  child records.
- **Full `investments.json`** (every SoI row with FV / cost / yield /
  identifier). Useful for the investor-report generator; belongs
  with it.

## Scoring integration

Two new factors and two new alerts land in `ingest/risk_weights.yaml`:

```yaml
industry_hhi:
  weight: 1.0
  source: portfolio.industry_hhi
  direction: lower_is_better
  curve:
    - [0.05,   0]   # ~20 effective industries
    - [0.08,  20]
    - [0.12,  40]
    - [0.20,  65]
    - [0.35,  90]   # ~3 effective industries
    - [1.00, 100]

non_accrual_fv:
  weight: 1.5
  source: portfolio.non_accrual_pct_fair_value
  direction: lower_is_better
  curve:
    - [0.005,   0]
    - [0.01,   15]
    - [0.02,   35]
    - [0.03,   55]
    - [0.05,   80]
    - [0.10,  100]

alerts:
  non_accrual_elevated: { severity: high,   threshold: >= 3% FV }
  industry_concentration: { severity: medium, threshold: HHI >= 0.20 }
```

Weights are intentionally conservative for v0 — HHI is capped at a
lower weight than leverage or dividend coverage, and will go up once
we also publish `top10_issuers_pct` as a companion single-name signal.

## Validation

After a full-universe extraction run, the headline numbers are:

- 52/52 BDCs produced `portfolio.json` without errors (13 seconds
  total end-to-end on a cached filings tree).
- 46/52 BDCs got a computed `industry_hhi`. The 6 misses are small /
  early-stage BDCs that don't tag any of the three industry layouts.
- 2/52 BDCs published a direct non-accrual % and were scored on it.
- Sanity-check totals: ARCC $34.1 B @ 2025-12-31, HHI 0.110 (top
  sector Software & Services 23.9%). Cross-checks against the
  published 10-K narrative.

See `reports/<DATE>/portfolio_summary.json` for per-run coverage.
