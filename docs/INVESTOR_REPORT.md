# Investor Briefs

This document specifies the per-BDC investor brief produced by
[`scripts/build_investor_report.py`](../scripts/build_investor_report.py):
what inputs it consumes, what it renders, and why the pieces are
arranged the way they are.

## Goals

The risk engine already produces structured JSON (`risk_<ticker>.json`
+ `alert_*.json`) that downstream systems can consume directly. What
was missing was a **human-readable** artifact an analyst, PM, or
client could read end-to-end in 60 seconds and form an opinion about
a BDC. The investor brief fills that gap with four constraints:

1. **Compose, don't re-fetch.** Everything in the brief has to come
   from artifacts already on disk — `summary.json`, `portfolio.json`,
   `timeseries.json`, `risk_<ticker>.json`, `alert_*.json`. We never
   hit EDGAR here; reruns are deterministic and offline-friendly.
2. **Markdown first.** Markdown renders in GitHub, Cursor, every
   email client after a trivial converter, and it diffs cleanly in
   git. HTML / PDF can be added later without changing the data
   contract.
3. **JSON mirror.** Every markdown brief has a parallel `.json`
   (schema `pricredit.investor_brief/v0`) carrying the same facts in
   a stable, machine-readable shape so CRM / dashboard integrations
   don't have to scrape markdown.
4. **No surprise sources.** Every number in the brief must be
   traceable to the input JSON. The factor audit table explicitly
   prints the dotted source path (`latest.asset_coverage_ratio`,
   `portfolio.industry_hhi`, …) used by the risk engine so readers
   can audit the composite themselves.

## Inputs

For each BDC with a `reports/<DATE>/risk_<TICKER>.json`, the generator
loads:

| Input | Purpose |
|---|---|
| `extracted/<cik>/facts/summary.json` | Canonical metrics for the latest filed period, derived ratios, `nav_trend` (QoQ / YoY), and the compact `portfolio:` block. |
| `extracted/<cik>/facts/timeseries.json` | Historical NAV / share observations for the sparkline + trend table (last 8 quarters). |
| `extracted/<cik>/portfolio/<accn>/portfolio.json` | Full SoI aggregates — top industries, affiliation mix, industry method, effective-N. |
| `reports/<DATE>/risk_<TICKER>.json` | Composite score, band, per-factor audit (raw / score / weight / contribution / source). |
| `reports/<DATE>/alert_RISK-<TICKER>-*.json` | Open alerts (severity, reason, description, triggers). |

If any of these are missing, the brief is still emitted with graceful
`n/a` fallbacks — the investor can see at a glance which signals
weren't available for the BDC.

## Outputs

Everything lands under `reports/<DATE>/briefs/`:

```
reports/<DATE>/briefs/
  <TICKER>.md         human-readable investor memo
  <TICKER>.json       pricredit.investor_brief/v0 payload
  index.md            universe roll-up, sorted by composite score
  index.json          pricredit.investor_brief_index/v0 payload
```

### Brief structure (markdown)

1. **Headline** — `TICKER — <band-headline> (score X/100, BAND)`. The
   band headline maps low→"solid credit profile",
   medium→"watch-list", high→"elevated risk", critical→"critical
   risk", unknown→"insufficient data".
2. **Header table** — company name, CIK, filing as-of (form +
   accession + filed date), brief-generated timestamp + run date.
3. **Risk snapshot** — composite, band, factors-used / total, and the
   three largest factor contributions (so the reader immediately
   sees *why* the BDC is where it is).
4. **Open alerts** — severity-sorted, each with its human description
   and the raw triggers (`source = value op threshold`) so a reader
   can verify the rule fired correctly.
5. **Financial snapshot** — 15-row table: NAV / share, net assets,
   total assets, total debt, asset coverage, D/E, investments at FV,
   FV/cost, NII, TII, distributions / share, dividends paid, NII /
   share, dividend coverage, PIK ratio.
6. **NAV trend** — QoQ + YoY pct changes (with direction arrow), a
   Unicode block-character sparkline of the last 8 observations, and
   the underlying table (period-end, NAV, form).
7. **Portfolio snapshot** — total FV, industries identified, industry
   HHI + effective-N, industry coverage % (what fraction of FV the
   HHI represents) and the extraction method
   (`pure_axis_fv` / `multiaxis_fv:…` / `concentration_pct_fv_benchmark`),
   largest industry share, non-accrual % (with source: direct_tag or
   "not tagged"), top-5 industries table, affiliation mix table.
8. **Factor audit** — one row per risk factor showing raw value,
   sub-score, weight, contribution, and the dotted source path; with
   a `(fallback)` marker when the primary source was missing and the
   fallback curve was used.
9. **Disclaimer footer.**

### Brief structure (JSON mirror)

```json
{
  "schema_version": "pricredit.investor_brief/v0",
  "generated_at_utc": "2026-04-18T21:29:14Z",
  "run_date": "2026-04-18-final",
  "ticker": "ARCC",
  "cik": "0001287750",
  "company_name": "ARES CAPITAL CORP  (ARCC)",
  "as_of_filing_end": "2025-12-31",
  "filing": { "form": "10-K", "accession": "...", "filed": "2026-02-04" },
  "risk": {
    "composite_score": 24.45,
    "band": "low",
    "n_factors_used": 8,
    "n_factors_missing": 0,
    "top_contributors": [ … 3 largest by |contribution| … ],
    "factors": [ … full factor audit from risk_<ticker>.json … ]
  },
  "alerts": [ { "alert_id", "reason", "severity", "description", "triggers" } ],
  "financial_snapshot": { … 15 metrics … },
  "nav_trend": {
    "qoq_pct_change": …, "yoy_pct_change": …,
    "history": [ { "end", "val", "form" }, … last 8 quarters … ]
  },
  "portfolio": {
    "as_of", "source_form", "source_accession",
    "total_fair_value", "n_industries",
    "industry_hhi", "industry_hhi_effective_n",
    "industry_coverage_pct", "industry_method",
    "largest_industry_pct",
    "top_industries": [ … top 5 … ],
    "affiliation_mix": { … },
    "non_accrual_pct_fair_value", "non_accrual_source"
  },
  "disclaimer": "…"
}
```

The JSON is byte-stable across reruns with identical inputs (we only
touch timestamps in `generated_at_utc`, which the consumer can ignore).

### Index roll-up

`index.md` / `index.json` aggregate the universe for the run date:

- Band distribution across all briefs (CRITICAL / HIGH / MEDIUM /
  LOW / UNKNOWN counts).
- BDCs sorted by composite score (highest risk first) with ticker,
  score, band, # alerts, NAV YoY, industry HHI, non-accrual %, and
  a relative link to the per-BDC markdown brief.

The index is the fastest way to answer "which BDCs need attention
today?" without opening every brief.

## CLI

```bash
# Default: latest reports/<DATE>/ folder with risk_*.json
scripts/build_investor_report.py

# Pin a run date and overwrite existing briefs
scripts/build_investor_report.py --date 2026-04-18 --force

# Subset of BDCs (same flags as compute_risk.py)
scripts/build_investor_report.py --tickers ARCC,MAIN,OBDC

# Dump markdown to stdout for quick inspection
scripts/build_investor_report.py --tickers ARCC --print

# Emit a JSON run-summary for orchestrators
scripts/build_investor_report.py --run-summary reports/2026-04-18/briefs_run_summary.json
```

Flags:

- `--extracted`, `--reports`, `--universe` — override default paths.
- `--date` — pin the run folder under `reports/`; if omitted, the
  newest folder containing `risk_*.json` is used.
- `--briefs-dir` — override the output folder (default
  `reports/<DATE>/briefs`).
- `--ticker` / `--tickers` / `--ciks` — restrict to a subset.
- `--force` — overwrite existing briefs (otherwise `<TICKER>.md` +
  `<TICKER>.json` are reused, counted under `reused`).
- `--print` — echo each brief to stdout as it's rendered.
- `--run-summary` — write a small JSON manifest (written, reused,
  errors, n_briefs).

## Integration with the daily orchestrator

`run-daily-pricredit.sh` invokes `build_investor_report.py` after
`compute_risk.py` when `SKIP_REPORTS` is 0 (the default) and neither
`SKIP_PARSE` nor `SKIP_RISK` is set. The step is opt-out via
`--skip-reports` / `SKIP_REPORTS=1` for pipelines that only want the
raw risk JSON.

Because the brief is composed from already-persisted artifacts, it
adds < 1 s of wall-clock to a full 52-BDC daily run. It's safe to
rerun idempotently.

## Scope limits (v0)

- **No PDF / HTML yet.** Markdown is the source format. Converting
  to HTML with `python-markdown` or to PDF via `weasyprint` is
  trivial when needed and doesn't require changes here.
- **No LLM summaries.** Every brief is deterministic. The
  `llm_summary` field exists on alerts for future use but briefs
  themselves don't call any LLM.
- **No comparative analytics yet.** Each brief covers one BDC at one
  run date. Peer comparisons (sector medians, percentile ranks)
  belong in a v1.
- **Fixed output order / sections.** Tailoring layouts per audience
  (IC memo vs client newsletter vs regulatory file) is deferred;
  the JSON mirror gives consumers full flexibility to re-render
  their own way.
