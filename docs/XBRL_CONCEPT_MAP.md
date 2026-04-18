# XBRL concept map — how PriCredit reads BDC filings

PriCredit consumes the SEC's XBRL company facts API:
`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`

A single companyfacts response can contain hundreds of us-gaap,
srt, dei, and custom-taxonomy tags. We translate a curated subset
into a stable set of **canonical metrics** used everywhere
downstream (time series, risk scores, investor reports). The
translation table lives in
[`scripts/_xbrl_concepts.py`](../scripts/_xbrl_concepts.py).

## Canonical metrics (v0)

### Instant (point-in-time) metrics

| Canonical | XBRL tag tried first | Unit |
|-----------|----------------------|------|
| `nav_per_share` | `us-gaap:NetAssetValuePerShare` | `USD/shares` |
| `total_assets` | `us-gaap:Assets` | `USD` |
| `total_liabilities` | `us-gaap:Liabilities` | `USD` |
| `net_assets` | `us-gaap:StockholdersEquity` | `USD` |
| `total_debt` | `us-gaap:LongTermDebt` | `USD` |
| `shares_outstanding` | `us-gaap:CommonStockSharesOutstanding` | `shares` |
| `investments_fair_value` | `us-gaap:InvestmentOwnedAtFairValue` | `USD` |
| `investments_cost` | `us-gaap:InvestmentOwnedAtCost` | `USD` |
| `asset_coverage_ratio` | `us-gaap:InvestmentCompanySeniorSecurityIndebtednessAssetCoverageRatio` | `pure` |

The asset-coverage tag is **BDC-specific**; it's the regulatory
ratio required to stay ≥150% (1.50) post-2018. ARCC and OBDC
tag it directly; MAIN often does not. When missing, use the
derived leverage proxy instead. Preferred-stock BDCs (e.g.,
Prospect Capital) also/instead tag the **Stock** variant
(`InvestmentCompanySeniorSecurityStockAssetCoverageRatio`) —
PriCredit tries both.

### Duration (period) metrics

| Canonical | XBRL tag tried first | Unit |
|-----------|----------------------|------|
| `net_investment_income` | `us-gaap:NetInvestmentIncome` | `USD` |
| `total_investment_income` | `us-gaap:GrossInvestmentIncomeOperating` | `USD` |
| `interest_income_pik` | `us-gaap:InterestIncomeOperatingPaidInKind` | `USD` |
| `dividend_income_pik` | `us-gaap:DividendIncomeOperatingPaidInKind` | `USD` |
| `interest_expense` | `us-gaap:InterestExpense` | `USD` |
| `distributions_per_share` | `us-gaap:CommonStockDividendsPerShareDeclared` | `USD/shares` |
| `dividends_paid` | `us-gaap:PaymentsOfDividends` | `USD` |
| `weighted_avg_shares_diluted` | `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding` | `shares` |
| `weighted_avg_shares_basic` | `us-gaap:WeightedAverageNumberOfSharesOutstandingBasic` | `shares` |
| `net_income_loss` | `us-gaap:NetIncomeLoss` | `USD` |

Each canonical metric accepts an ordered list of candidate tags;
the first tag with observations wins. If every explicit tag misses,
we fall back to regex patterns (`CONCEPT_PATTERNS`) across *all*
taxonomies, which catches BDC-specific custom tags like
`arcc:NetAssetValuePerShare`.

### Tag variation across the BDC universe

Several canonical metrics are filed under materially different us-gaap
tags across BDCs. We enumerate every variant observed in the cached
universe so one parser run covers as many filers as possible. Coverage
percentages below are over the 52 BDCs currently in `bdc/_cache/`.

| Canonical | Candidate tags (priority order) | Coverage |
|-----------|---------------------------------|----------|
| `interest_income_pik` | `InterestIncomeOperatingPaidInKind` → `PaidInKindInterest` → `InterestAndDividendIncomeOperatingPaidInKind`¹ | 73% |
| `dividend_income_pik` | `DividendIncomeOperatingPaidInKind` → `DividendsPaidinkind`² → `DividendsCommonStockPaidinkind` | 17% |
| `dividends_paid` | `PaymentsOfDividends` → `PaymentsOfDividendsCommonStock` → `DividendsCommonStockCash` → `PaymentsOfOrdinaryDividends` → `DividendsCash` → `PaymentsOfCapitalDistribution` | 90% |
| `asset_coverage_ratio` | `InvestmentCompanySeniorSecurityIndebtednessAssetCoverageRatio` → `InvestmentCompanySeniorSecurityStockAssetCoverageRatio` | 44% |

¹ The `InterestAndDividend…PaidInKind` tag is a *combined* interest +
dividend PIK figure used by ~13 filers (e.g., Investcorp, Hercules).
We intentionally map it to the interest side only; the derivation
`pik_income_ratio = (interest_pik + dividend_pik) / total_investment_income`
therefore still produces a correct total PIK figure without
double-counting. The dividend-side regex fallback is explicitly
anchored so it does *not* match the combined tag.

² Note the lower-cased "inkind" spelling used by ARCC and a handful
of other filers. The regex fallbacks are case-insensitive and
anchored so that a tag like `InterestAndDividendIncomeOperatingPaidInKind`
only resolves to the interest side.

## Derived metrics

Computed from the resolved primitives:

| Derived | Formula | Why it matters |
|---------|---------|----------------|
| `leverage_debt_to_equity` | `total_debt / net_assets` | Leverage proxy when `asset_coverage_ratio` is missing. |
| `debt_to_assets` | `total_debt / total_assets` | Balance-sheet leverage. |
| `fair_value_to_cost` | `investments_fair_value / investments_cost` | <1 implies unrealized losses. |
| `pik_income_ratio` | `(interest_pik + dividend_pik) / total_investment_income` | High ratio = cash-light income; harder to sustain distributions. |
| `dividend_coverage_nii_over_divs` | `net_investment_income / dividends_paid` | <1 implies the BDC is returning capital. |
| `nii_per_share` | `net_investment_income / weighted_avg_shares_*` | Per-share earnings power. |

NAV trend is reported separately in `summary.json`:

```json
"nav_trend": {
  "qoq": {"from_end": "...", "to_end": "...", "pct_change": ...},
  "yoy": {"from_end": "...", "to_end": "...", "pct_change": ...}
}
```

## Restatement handling

For each `(metric, period_start, period_end)`, the parser keeps the
observation with the **most recent** `filed` date. That means a
10-K/A amendment automatically supersedes the number originally
filed on the 10-K. Earlier versions are dropped silently from the
time series but still accessible via raw companyfacts JSON.

## Extending the map

When adding a BDC whose metric is missing:

1. Fetch its companyfacts JSON:
   `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`
2. `jq '.facts | keys'` to see taxonomies.
3. `jq '.facts["us-gaap"] | keys' | grep -i <keyword>` — find the tag.
4. Add the `(taxonomy, tag, unit)` triple to the corresponding list
   in `scripts/_xbrl_concepts.py`. Keep the most-common variant
   first so other BDCs in your run benefit too.

Keep an eye on `reports/<DATE>/parse_summary.json` —
`coverage_by_metric.unresolved_counts` tells you which metrics are
missing across the universe and are worth mapping next.
