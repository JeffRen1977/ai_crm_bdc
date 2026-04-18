# BDC primer (what AI-CRM actually reads)

A **Business Development Company (BDC)** is a US closed-end
investment vehicle, defined under sections 2(a)(48) and 54-65 of the
Investment Company Act of 1940, that invests primarily in debt and
equity of private US middle-market companies. Publicly traded BDCs
must register with the SEC and file the same investor disclosures
other US public companies do, plus some regime-specific items.

Authoritative overview from the SEC:
<https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/publicly-traded-business-development-companies-bdcs-investor-bulletin>

## Forms we care about

| Form | Frequency | Why AI-CRM cares |
|------|-----------|------------------|
| **N-54A** | Once | Election to be regulated as a BDC. Filing this once is our strongest signal that an SEC registrant **is** a BDC. We use it to seed `bdc_universe.json`. |
| **N-54C** | Once | Withdrawal of BDC election. Used to **retire** a BDC from the universe. |
| **10-K** | Annual | Full audited financials, Schedule of Investments at FY-end, risk factors, non-accrual disclosures, asset-coverage ratio. |
| **10-Q** | Quarterly | Unaudited financials + Schedule of Investments (what the BDC holds at quarter-end). The goldmine for portfolio views. |
| **8-K** | Event-driven | Material events: dividend changes, credit facility amendments, executive changes, non-compliance notices. |
| **DEF 14A** | Annual | Proxy statement; executive compensation, related-party transactions. |
| **N-2** | Occasional | Registration statement for new share issuance (often a dilution signal). |
| **SC 13G/13D** | Occasional | Beneficial ownership — useful when we build peer-ownership graphs. |

## What lives where in a 10-Q

Practical treasure map for the parser:

- **Consolidated Statement of Assets and Liabilities** → NAV, leverage
  ratio inputs, asset coverage per RIC rules (must be ≥150% post-2018
  for most BDCs).
- **Consolidated Statement of Operations** → Total investment income
  split into interest, PIK interest, dividends, fees. PIK% tells us how
  much income is paper vs cash.
- **Consolidated Schedule of Investments** → loan-level table with
  borrower, industry, coupon, floor, maturity, cost, fair value, and
  non-accrual footnote markers. This is the Portfolio Management
  module's primary source.
- **Footnotes — Non-accrual status / restructurings** → explicit list
  of impaired positions; critical for Risk Management.
- **Management's Discussion and Analysis** → forward-looking color on
  originations, repayments, and market conditions.

## Signals we extract (v1 design target)

- **NAV per share trend** (QoQ and YoY), from XBRL
  `us-gaap:NetAssetValuePerShare` when tagged, else from the Statement
  of Assets and Liabilities.
- **Leverage / asset coverage** from XBRL
  `us-gaap:LongTermDebtNoncurrent` + equity inputs.
- **Non-accrual % of fair value** — parsed from Schedule of
  Investments footnotes.
- **PIK income ratio** — PIK interest ÷ total investment income.
- **Dividend coverage** — Net Investment Income per share ÷
  Distributions per share.
- **Industry concentration** — sum of fair value by GIC / SIC bucket
  from the Schedule of Investments.
- **Maturity wall** — % of portfolio fair value maturing in the next
  12 / 24 / 36 months.

Each signal carries a citation: `cik`, `accession`, `form`,
`page-or-concept`, so reports can link back to the underlying filing.
