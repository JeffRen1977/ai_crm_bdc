"""
Canonical BDC metric catalog for PriCredit.

The `CONCEPT_MAP` maps a PriCredit canonical metric name to an ordered
list of `(taxonomy, tag, unit)` triples we try in priority order against
EDGAR's companyfacts JSON. The first triple that has observations wins.

Every tag below was verified against at least one real BDC's filings,
and most lists were validated by surveying all BDCs in the current
universe (see `docs/XBRL_CONCEPT_MAP.md` for the coverage matrix).
When adding a BDC that uses different tags, prefer extending the
ordered list here over writing ticker-specific code in parse_filings.py.

Structure of a companyfacts fact (for reference):
    facts["us-gaap"]["NetAssetValuePerShare"]["units"]["USD/shares"]
        -> [{"end":"2024-12-31","val":19.89,"accn":"...","fy":2024,
             "fp":"FY","form":"10-K","filed":"2025-02-06"}, ...]

Units are the second lookup key; we record which unit we used so that
downstream consumers can sanity-check.
"""
from __future__ import annotations

import re
from typing import Callable

# ---------------------------------------------------------------------------
# Canonical metrics — grouped by purpose.
# ---------------------------------------------------------------------------

# Instant / point-in-time (balance-sheet style) metrics.
INSTANT_METRICS = {
    "nav_per_share": [
        ("us-gaap", "NetAssetValuePerShare", "USD/shares"),
    ],
    "total_assets": [
        ("us-gaap", "Assets", "USD"),
    ],
    "total_liabilities": [
        ("us-gaap", "Liabilities", "USD"),
    ],
    "net_assets": [
        ("us-gaap", "StockholdersEquity", "USD"),
        ("us-gaap", "NetAssets", "USD"),
    ],
    "total_debt": [
        ("us-gaap", "LongTermDebt", "USD"),
        ("us-gaap", "LongTermDebtNoncurrent", "USD"),
        ("us-gaap", "DebtInstrumentCarryingAmount", "USD"),
    ],
    "shares_outstanding": [
        ("us-gaap", "CommonStockSharesOutstanding", "shares"),
        ("dei", "EntityCommonStockSharesOutstanding", "shares"),
    ],
    "investments_fair_value": [
        ("us-gaap", "InvestmentOwnedAtFairValue", "USD"),
        ("us-gaap", "InvestmentsFairValueDisclosure", "USD"),
    ],
    "investments_cost": [
        ("us-gaap", "InvestmentOwnedAtCost", "USD"),
    ],
    # BDC-required ratio; filed directly by most BDCs. Pure ratio (e.g., 2.03).
    # Most BDCs report Indebtedness coverage; preferred-stock BDCs also/instead
    # tag the Stock variant (e.g., Prospect Capital).
    "asset_coverage_ratio": [
        ("us-gaap", "InvestmentCompanySeniorSecurityIndebtednessAssetCoverageRatio", "pure"),
        ("us-gaap", "InvestmentCompanySeniorSecurityStockAssetCoverageRatio", "pure"),
    ],
}

# Duration (period) metrics. Observations carry both `start` and `end`.
DURATION_METRICS = {
    "net_investment_income": [
        ("us-gaap", "NetInvestmentIncome", "USD"),
        ("us-gaap", "InvestmentIncomeNet", "USD"),
        ("us-gaap", "InvestmentIncomeOperatingAfterExpenseAndTax", "USD"),
    ],
    "total_investment_income": [
        ("us-gaap", "GrossInvestmentIncomeOperating", "USD"),
        ("us-gaap", "InvestmentIncomeInvestment", "USD"),
        ("us-gaap", "InvestmentIncome", "USD"),
    ],
    # PIK interest income. Some filers (Investcorp, Hercules, etc.) report a
    # *combined* interest+dividend PIK under "InterestAndDividend…"; we map it
    # here so `derive_pik_income_ratio` can still compute a total PIK figure
    # (dividend_income_pik will simply resolve to None in that case).
    "interest_income_pik": [
        ("us-gaap", "InterestIncomeOperatingPaidInKind", "USD"),
        ("us-gaap", "PaidInKindInterest", "USD"),
        ("us-gaap", "InterestAndDividendIncomeOperatingPaidInKind", "USD"),
    ],
    # PIK dividend income. BDCs use at least three spellings of the tag name
    # (note the lowercased "inkind" variant from ARCC and others).
    "dividend_income_pik": [
        ("us-gaap", "DividendIncomeOperatingPaidInKind", "USD"),
        ("us-gaap", "DividendsPaidinkind", "USD"),
        ("us-gaap", "DividendsCommonStockPaidinkind", "USD"),
    ],
    "interest_expense": [
        ("us-gaap", "InterestExpense", "USD"),
    ],
    "distributions_per_share": [
        ("us-gaap", "CommonStockDividendsPerShareDeclared", "USD/shares"),
        ("us-gaap", "InvestmentCompanyDistributionToShareholdersPerShare", "USD/shares"),
        ("us-gaap", "CommonStockDividendsPerShareCashPaid", "USD/shares"),
    ],
    # Cash-flow dividends/distributions paid. Tag variation across BDCs is
    # wide; the first four cover ~95% of the universe (the last two mop up
    # partnership-structured filers and a handful of older filings).
    "dividends_paid": [
        ("us-gaap", "PaymentsOfDividends", "USD"),
        ("us-gaap", "PaymentsOfDividendsCommonStock", "USD"),
        ("us-gaap", "DividendsCommonStockCash", "USD"),
        ("us-gaap", "PaymentsOfOrdinaryDividends", "USD"),
        ("us-gaap", "DividendsCash", "USD"),
        ("us-gaap", "PaymentsOfCapitalDistribution", "USD"),
    ],
    "weighted_avg_shares_diluted": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
    ],
    "weighted_avg_shares_basic": [
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", "shares"),
    ],
    "net_income_loss": [
        ("us-gaap", "NetIncomeLoss", "USD"),
    ],
}

CONCEPT_MAP: dict[str, list[tuple[str, str, str]]] = {
    **INSTANT_METRICS,
    **DURATION_METRICS,
}

# Whether a metric is point-in-time or a period flow.
METRIC_KIND: dict[str, str] = {
    **{k: "instant" for k in INSTANT_METRICS},
    **{k: "duration" for k in DURATION_METRICS},
}

# ---------------------------------------------------------------------------
# Regex fallbacks — tried across ALL taxonomies when no explicit tag hit.
# Each entry: (canonical_metric, compiled_pattern, expected_unit).
# ---------------------------------------------------------------------------

CONCEPT_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("nav_per_share",          re.compile(r"NetAssetValuePerShare$"), "USD/shares"),
    ("net_investment_income",  re.compile(r"NetInvestmentIncome$"), "USD"),
    ("total_debt",             re.compile(r"^LongTermDebt$"), "USD"),
    ("asset_coverage_ratio",   re.compile(r"AssetCoverage", re.IGNORECASE), "pure"),
    # PIK casing varies ("PaidInKind" vs "Paidinkind"); keep these matchers
    # case-insensitive so filer quirks don't silently drop the fact.
    #
    # IMPORTANT: anchor each pattern so the *combined* "InterestAndDividend…"
    # tag is not matched by the dividend-side fallback (it would double-count
    # the interest portion against the interest side in derive_pik_income_ratio).
    # - Interest side:  tag starts with "Interest" (not "InterestAnd…"), or
    #                   contains "PaidInKindInterest".
    # - Dividend side:  tag starts with "Dividend", or contains
    #                   "PaidInKindDividend".
    ("interest_income_pik",    re.compile(r"^Interest(?!AndDividend).*Paid[Ii]n[Kk]ind|Paid[Ii]n[Kk]indInterest", re.IGNORECASE), "USD"),
    ("dividend_income_pik",    re.compile(r"^Dividend.*Paid[Ii]n[Kk]ind|Paid[Ii]n[Kk]indDividend", re.IGNORECASE), "USD"),
    # Dividends-paid cash-flow mopup (any tag whose name looks like payments
    # of dividends/distributions). We already enumerate the common tags in
    # CONCEPT_MAP; this regex only runs when none of those resolved.
    ("dividends_paid",         re.compile(r"^Payments[A-Za-z]*(?:Dividend|Distribution)", re.IGNORECASE), "USD"),
]


# ---------------------------------------------------------------------------
# Derived metrics — computed from the resolved primitives.
# Each derivation runs against `latest: {metric -> {"val": number, ...}}`.
# Return (name, value, inputs_used) or None if inputs are missing.
# ---------------------------------------------------------------------------

def _get(latest: dict, key: str) -> float | None:
    v = latest.get(key)
    if not v:
        return None
    val = v.get("val")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def derive_leverage_debt_to_equity(latest: dict) -> dict | None:
    debt = _get(latest, "total_debt")
    equity = _get(latest, "net_assets")
    if debt is None or equity in (None, 0):
        return None
    return {"name": "leverage_debt_to_equity", "val": debt / equity,
            "inputs": ["total_debt", "net_assets"]}


def derive_debt_to_assets(latest: dict) -> dict | None:
    debt = _get(latest, "total_debt")
    assets = _get(latest, "total_assets")
    if debt is None or assets in (None, 0):
        return None
    return {"name": "debt_to_assets", "val": debt / assets,
            "inputs": ["total_debt", "total_assets"]}


def derive_fair_vs_cost(latest: dict) -> dict | None:
    fv = _get(latest, "investments_fair_value")
    cost = _get(latest, "investments_cost")
    if fv is None or cost in (None, 0):
        return None
    return {"name": "fair_value_to_cost", "val": fv / cost,
            "inputs": ["investments_fair_value", "investments_cost"]}


def derive_pik_income_ratio(latest: dict) -> dict | None:
    pik_i = _get(latest, "interest_income_pik") or 0.0
    pik_d = _get(latest, "dividend_income_pik") or 0.0
    tot = _get(latest, "total_investment_income")
    if tot in (None, 0):
        return None
    pik = pik_i + pik_d
    if pik <= 0:
        return None
    return {"name": "pik_income_ratio", "val": pik / tot,
            "inputs": ["interest_income_pik", "dividend_income_pik",
                       "total_investment_income"]}


def derive_dividend_coverage(latest: dict) -> dict | None:
    nii = _get(latest, "net_investment_income")
    divs = _get(latest, "dividends_paid")
    if nii is None or divs in (None, 0):
        return None
    return {"name": "dividend_coverage_nii_over_divs", "val": nii / divs,
            "inputs": ["net_investment_income", "dividends_paid"]}


def derive_nii_per_share(latest: dict) -> dict | None:
    nii = _get(latest, "net_investment_income")
    shares = (_get(latest, "weighted_avg_shares_diluted")
              or _get(latest, "weighted_avg_shares_basic")
              or _get(latest, "shares_outstanding"))
    if nii is None or shares in (None, 0):
        return None
    return {"name": "nii_per_share", "val": nii / shares,
            "inputs": ["net_investment_income", "weighted_avg_shares_*"]}


DERIVATIONS: list[Callable[[dict], dict | None]] = [
    derive_leverage_debt_to_equity,
    derive_debt_to_assets,
    derive_fair_vs_cost,
    derive_pik_income_ratio,
    derive_dividend_coverage,
    derive_nii_per_share,
]


# ---------------------------------------------------------------------------
# QoQ / YoY helpers on an instant-metric timeseries.
# The caller passes a *sorted-by-end* list of observations.
# ---------------------------------------------------------------------------

def pct_change(current: float, prior: float) -> float | None:
    if prior in (None, 0):
        return None
    return (current - prior) / abs(prior)


def find_prior(series: list[dict], ref_end: str, months_ago: int) -> dict | None:
    """Find an observation whose `end` is approximately `months_ago` before
    `ref_end`. We just return the observation with `end` closest to the
    target date (tolerance: ±45 days)."""
    from datetime import date, timedelta

    def _parse(s: str) -> date | None:
        try:
            y, m, d = s.split("-")
            return date(int(y), int(m), int(d))
        except Exception:
            return None

    ref = _parse(ref_end)
    if not ref:
        return None
    # target = ref - months_ago months (roughly)
    target_days = months_ago * 30
    target = ref - timedelta(days=target_days)
    tolerance = timedelta(days=45)
    best = None
    best_gap: timedelta | None = None
    for obs in series:
        end = _parse(obs.get("end", ""))
        if not end:
            continue
        if end >= ref:
            continue
        gap = abs(end - target)
        if gap > tolerance:
            continue
        if best_gap is None or gap < best_gap:
            best = obs
            best_gap = gap
    return best
