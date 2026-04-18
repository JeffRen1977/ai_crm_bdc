# PriCredit risk model (v1)

> Public-data, heuristic BDC risk score. Informational only.
> No backtest. No predictive claim. The file is small on purpose —
> when the numbers surprise you, the right answer is to adjust
> `ingest/risk_weights.yaml` and re-score, not to question the code.

## 1. Why a heuristic?

A BDC's credit health is a small set of widely agreed-upon signals
(regulatory asset coverage, NAV trajectory, NII-vs-distributions,
PIK share, unrealized marks). Those signals are reported in XBRL on
EDGAR with stable tags, so we can compute them deterministically.
A tuned statistical model would be better, but needs labeled
distress outcomes we don't yet have. Until then a transparent
hand-set curve beats an opaque black box.

## 2. Pipeline

```
extracted/<cik>/facts/summary.json
          │
          ▼
 score_factor(name, cfg, summary)   <-- one per entry in factors:
          │                             YAML. Piecewise-linear.
          ▼
 composite = Σ(w_i · s_i) / Σ(w_i)      over factors with data
          │
          ▼
 band = first threshold in bands:      low | medium | high | critical

 evaluate_alert(rule, cfg, summary)     rule-based, severity-ranked
          │
          ▼
 dedupe_by_group(alerts)                within a group, highest wins
```

The scorer and the alert engine are intentionally decoupled: a
critical regulatory breach always surfaces as an alert even if the
composite happens to be low (e.g., because several factors are
missing for that BDC).

## 3. Factors shipped in v0

All curves live in `ingest/risk_weights.yaml` and are the source of
truth. This section explains the reasoning.

### 3.1 `asset_coverage`   (weight 2.0)

> **Input:** `latest.asset_coverage_ratio` (tagged directly by most
> BDCs under `cef:AssetCoverage` etc.)
> **Fallback:** `derived.leverage_debt_to_equity`

The BDC regulatory minimum is **150%** asset coverage (Investment
Company Act §61, amended 2018). Breaching it forces distribution
suspensions and debt incurrence restrictions. Curve:

| raw   | score | interpretation                       |
|-------|-------|--------------------------------------|
| ≥2.00 | 0     | clear cushion                        |
| 1.80  | 20    | normal operating range               |
| 1.65  | 40    | approaching the floor                |
| 1.55  | 60    | near regulatory minimum              |
| 1.50  | 80    | at the floor                         |
| <1.50 | 100   | regulatory breach → critical alert   |

Weight is 2× because (a) it's the only regulatory hard limit in the
dataset, and (b) it aggregates leverage + capital adequacy into one
ratio.

When the BDC doesn't tag asset_coverage explicitly, fall back to
debt/equity derived from balance-sheet items. 0.70 ≈ 2.00× coverage
(since coverage = (assets) / debt ≈ 1/(D/E·(1-D/A))); thresholds are
hand-aligned to produce comparable scores.

### 3.2 `fair_value_to_cost`   (weight 1.0)

FV/Cost of the investment portfolio. >1.00 means unrealized gains;
<1.00 means the manager marked the book down. Deep deterioration
(0.90) is a strong leading indicator of eventual realized losses.

### 3.3 `pik_income_ratio`   (weight 1.0)

(PIK interest + PIK dividends) ÷ total investment income.
PIK = paid-in-kind: borrower pays interest by increasing principal
rather than in cash. High PIK share = cash-light earnings + usually
a sign of restructured loans. 5% is routine; 20%+ is the crisis band.

### 3.4 `dividend_coverage`   (weight 1.5)

Net investment income ÷ dividends paid. Under 1.0 means the BDC is
paying out of capital (return-of-capital distributions), which is
unsustainable and usually precedes a dividend cut.

### 3.5 `nav_yoy`   (weight 1.5)

NAV per share YoY. The single clearest structural signal — a healthy
BDC can be flat to slightly up; persistent erosion means realized
credit losses are outpacing earnings. Weighted 1.5× because it
integrates everything else over a year.

### 3.6 `nav_qoq`   (weight 1.0)

Short-horizon NAV drift. Noisy quarter to quarter, but useful as an
early warning between 10-Ks.

## 4. Composite → band

```
band thresholds (upper bounds, exclusive):
  low       < 25
  medium    < 50
  high      < 75
  critical  ≥ 75
```

Missing factors are **excluded**, not imputed; the remaining weights
renormalize. This avoids punishing a BDC whose XBRL taxonomy is
incomplete while still letting the operator see which factors fired
in the per-BDC scorecard.

## 5. Alert rules

Alerts are independent of the composite. Each rule has `any_of`
conditions; if any condition is met, the alert fires.

| Rule                              | Severity | Group    |
|-----------------------------------|----------|----------|
| `leverage_breach`                 | critical | leverage |
| `leverage_elevated`               | high     | leverage |
| `dividend_coverage_shortfall`     | high     | —        |
| `nav_decline`                     | medium   | —        |
| `pik_overreliance`                | medium   | —        |
| `unrealized_loss`                 | medium   | —        |

Within a `group`, only the highest-severity alert that fires is
emitted. Example: if `leverage_breach` fires, `leverage_elevated` is
suppressed to avoid double-alerting for the same condition.

`requires_missing`: a condition is only evaluated when a named
source is absent. Used so the leverage fallback conditions only fire
when `latest.asset_coverage_ratio` isn't tagged by the BDC.

## 6. Restatement policy

Inherited from `parse_filings.py`: when two observations cover the
same period, the later-filed one wins (10-K/A supersedes 10-K). The
risk engine reads `summary.json.latest.*`, which already reflects
this deduping.

## 7. Output contract

### Per BDC — `reports/<DATE>/risk_<ticker>.json`

```jsonc
{
  "schema_version": "pricredit.compute_risk/v1",
  "weights_version": "v1",
  "cik": "0001287750",
  "ticker": "ARCC",
  "composite_score": 24.14,
  "band": "low",
  "n_factors_used": 6,
  "n_factors_missing": 0,
  "factors": [
    {
      "name": "asset_coverage",
      "weight": 2.0,
      "raw_value": 1.89,
      "used_source": "latest.asset_coverage_ratio",
      "used_fallback": false,
      "curve": [[2.00,0],[1.80,20],[1.65,40],[1.55,60],[1.50,80],[0,100]],
      "direction": "higher_is_better",
      "score": 11.0,
      "weight_share": 0.2353,
      "contribution": 2.59
    },
    ...
  ],
  "alerts": [...],
  "disclaimer": "..."
}
```

### Roll-up — `reports/<DATE>/risk_summary.json`

Same fields, sorted by `composite_score` descending, with
`band_counts` and `alerts_emitted` totals.

### Alerts — `reports/<DATE>/alert_RISK-<ticker>-<YYYYMMDD>-NNN.json`

Same shape as the idvault alert format (`alert_id`, `case_id`,
`severity`, `alert_reason`, `triggers`, `description`, disclaimer).
Makes the idvault email dispatcher reusable with minimal changes
when we're ready to notify on BDC risk.

## 8. How to tune

1. Edit `ingest/risk_weights.yaml` (weights / curve points /
   thresholds / adding or removing factors).
2. Re-run: `scripts/compute_risk.py --tickers ARCC,MAIN,OBDC --print --force`.
3. The scorecards' `curve` field captures the exact curve used, so
   historical scorecards are always reproducible from their file
   contents alone.

## 9. Known limitations (roadmap)

- **Non-accrual %** is live for the 2/52 BDCs that tag the concept
  directly (ARCC, CION). For everyone else the factor is excluded
  from the composite and the `non_accrual_elevated` alert can't fire
  — a v1 HTML-table SoI parser in `extract_portfolio.py` is the
  planned fix.
- **Industry concentration (HHI)** is live for 46/52 BDCs via
  `extract_portfolio.py`'s inline-XBRL parser. See
  [`PORTFOLIO_MODEL.md`](PORTFOLIO_MODEL.md) for coverage caveats —
  the HHI is measured over industry-tagged investments, not the
  entire portfolio, so when `industry_coverage_pct` is low the
  number is directional, not absolute.
- **Cross-filing validation**: the engine trusts XBRL facts as
  reported; it does not yet detect tag-taxonomy drift between
  filings (e.g., a BDC switching from `us-gaap:Investments` to a
  custom tag).
- **No peer normalization**: scores are absolute, not relative to
  the BDC cohort. A cohort-relative z-score view is a cheap
  follow-up.
- **No backtesting**: weights and curves are hand-set. Once we have
  outcomes data (dividend cuts, delistings, restructurings) we can
  calibrate.

## 10. Not investment advice

Every scorecard and every roll-up carries a disclaimer. The risk
engine is a monitoring and triage tool, not a buy/sell signal.
