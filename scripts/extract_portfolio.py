#!/usr/bin/env python3
"""
Extract portfolio-level credit signals from each BDC's most recent
10-K / 10-Q Schedule of Investments via inline XBRL.

For each BDC we:
  1. Pick the most recent 10-Q (fallback: 10-K) with a locally-saved
     primary document (fetched by scripts/fetch_filings.py).
  2. Parse the iXBRL facts and contexts in a single streaming pass
     using scripts/_soi_parser.py.
  3. Aggregate `us-gaap:InvestmentOwnedAtFairValue` by axis member:
       * `EquitySecuritiesByIndustryAxis`  -> industry HHI, top industries
       * `InvestmentTypeAxis`              -> first lien / second lien / equity mix
       * `InvestmentIdentifierAxis`        -> top-N single-issuer exposures
  4. Try to read a *direct* `non_accrual_pct_fair_value` fact (only
     ARCC and CION tag this today). Otherwise emit null and explain.
  5. Write two artefacts per BDC:
       extracted/<cik>/portfolio/<accession>/portfolio.json  (aggregates)
       extracted/<cik>/portfolio/<accession>/source.json     (filing meta)
     …and merge a `portfolio:` block into `extracted/<cik>/facts/summary.json`
     so `compute_risk.py` can consume it without a second fetch.
  6. Write a global run summary to reports/<DATE>/portfolio_summary.json.

Usage:
    scripts/extract_portfolio.py --ticker ARCC --print
    scripts/extract_portfolio.py --tickers ARCC,MAIN,OBDC,GBDC
    scripts/extract_portfolio.py            # all public BDCs with cached filings

v0 limitations (documented in docs/PORTFOLIO_MODEL.md):
  * Non-accrual percent is null for BDCs that don't tag a direct
    concept (~96% of the universe). A text-level SoI parser is the
    next step for that gap.
  * Loan-type normalisation is heuristic; see the member mapping
    table. Unmapped members fall into "Other".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _soi_parser import (  # noqa: E402
    AXIS_INDUSTRY,
    _clean_member_label,
    aggregate_by_axis_best_signature,
    aggregate_fv_by_axis,
    compute_hhi,
    iter_facts,
    load_primary,
    parse_contexts,
    pick_latest_period,
    top_n,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"
DEFAULT_EXTRACTED = ROOT / "extracted"
DEFAULT_FILINGS = ROOT / "filings"


# ---------------------------------------------------------------------------
# Percentage-based industry decomposition (Main Street style).
#
# MAIN and a few other BDCs don't tag per-industry FV — instead they
# publish `us-gaap:ConcentrationRiskPercentage1` under
# (industry_axis + benchmark=FairValueMember) contexts. Each value is
# a share of the total portfolio; we can compute HHI from the shares
# directly.
# ---------------------------------------------------------------------------

_CONC_PCT_CONCEPT = "us-gaap:ConcentrationRiskPercentage1"
_BENCHMARK_AXIS = "us-gaap:ConcentrationRiskByBenchmarkAxis"
_FV_BENCHMARK_MEMBER_RE = re.compile(r"InvestmentOwnedAtFairValueMember", re.IGNORECASE)


def _industry_shares_from_concentration_percent(text, contexts, period_end):
    """Fallback: MAIN-style filers tag only concentration percentages."""
    shares: dict[str, float] = {}
    for fact in iter_facts(text, concepts={_CONC_PCT_CONCEPT}):
        ctx = contexts.get(fact.context_ref)
        if not ctx or (period_end and ctx.period_end != period_end):
            continue
        industry = ctx.members.get(AXIS_INDUSTRY)
        if not industry:
            continue
        bench = ctx.members.get(_BENCHMARK_AXIS, "")
        if not _FV_BENCHMARK_MEMBER_RE.search(bench):
            continue
        # Scale: filer may tag as 0.1234 or 12.34. Heuristic: values > 1
        # treated as percent points.
        v = fact.value
        if v > 1.0:
            v = v / 100.0
        shares[industry] = shares.get(industry, 0.0) + v
    return shares


# Concepts that carry a direct non-accrual percent (as opposed to the
# filer reporting the number in prose). We search custom taxonomies for
# any tag whose local name matches these patterns.
NON_ACCRUAL_PCT_FV_PATTERNS = [
    re.compile(r"NonAccrual.*Percent(?:Of)?FairValue", re.IGNORECASE),
    re.compile(r"PercentFairValueNonAccrual", re.IGNORECASE),
]
NON_ACCRUAL_PCT_COST_PATTERNS = [
    re.compile(r"NonAccrual.*PercentOfCost", re.IGNORECASE),
]
NON_ACCRUAL_COUNT_PATTERNS = [
    re.compile(r"InvestmentOwnedNumberOfLoansOnNonAccrualStatus", re.IGNORECASE),
    re.compile(r"NumberOf.*(?:InvestmentsOn|LoansOn)NonAccrual", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Filing selection.
# ---------------------------------------------------------------------------

def _find_latest_filing(cik: str, filings_root: Path,
                        forms: tuple[str, ...] = ("10-Q", "10-K")
                       ) -> Optional[dict]:
    """Return the meta-dict for the freshest locally-saved primary doc."""
    cik_dir = filings_root / cik
    if not cik_dir.is_dir():
        return None
    best: Optional[tuple[tuple, dict]] = None
    for acc in cik_dir.iterdir():
        meta_p = acc / "meta.json"
        if not meta_p.exists():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        form = meta.get("form")
        if form not in forms:
            continue
        if not meta.get("primary_saved"):
            continue
        primary = meta.get("local_primary_path")
        if not primary or not Path(primary).exists():
            continue
        fd = meta.get("filing_date") or meta.get("report_date") or ""
        # Prefer 10-Q over 10-K when filing dates tie — the 10-Q is the
        # most-current SoI snapshot.
        form_rank = 1 if form == "10-Q" else 0
        key = (fd, form_rank)
        if best is None or key > best[0]:
            best = (key, meta)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Core extraction.
# ---------------------------------------------------------------------------

def extract_non_accrual(ixbrl_text: str, period_end: str,
                        contexts: dict) -> dict:
    """Look for a tagged non-accrual concept and return the latest value."""
    # Build a quick regex over all nonFraction concepts we care about.
    found: dict[str, Optional[dict]] = {
        "pct_fair_value": None, "pct_cost": None, "count": None,
    }

    def _match(cat_patterns, concept):
        return any(p.search(concept) for p in cat_patterns)

    best_for = {"pct_fair_value": None, "pct_cost": None, "count": None}
    for fact in iter_facts(ixbrl_text):
        cat = None
        if _match(NON_ACCRUAL_PCT_FV_PATTERNS, fact.concept):
            cat = "pct_fair_value"
        elif _match(NON_ACCRUAL_PCT_COST_PATTERNS, fact.concept):
            cat = "pct_cost"
        elif _match(NON_ACCRUAL_COUNT_PATTERNS, fact.concept):
            cat = "count"
        if not cat:
            continue
        ctx = contexts.get(fact.context_ref)
        if not ctx:
            continue
        # prefer exact period match, but keep any non-accrual read we find
        is_period_match = ctx.period_end == period_end
        rec = {
            "concept": fact.concept,
            "value": fact.value,
            "period_end": ctx.period_end,
            "raw": fact.raw_text,
        }
        prior = best_for[cat]
        if prior is None:
            best_for[cat] = (is_period_match, rec)
        else:
            prior_match, _ = prior
            # Any period-match wins over no-match; otherwise keep the
            # newest period_end.
            if is_period_match and not prior_match:
                best_for[cat] = (True, rec)
            elif is_period_match == prior_match:
                cur_end = rec["period_end"] or ""
                old_end = prior[1]["period_end"] or ""
                if cur_end > old_end:
                    best_for[cat] = (is_period_match, rec)

    for k, v in best_for.items():
        if v is not None:
            found[k] = v[1]

    return found


def extract_one_bdc(bdc: dict, filings_root: Path, extracted_root: Path,
                    force: bool = False) -> dict:
    """Extract portfolio aggregates for one BDC; return a summary row."""
    cik = bdc["cik"]
    ticker = (bdc.get("primary_ticker") or (bdc.get("tickers") or [""])[0] or "").upper()
    label = ticker or cik

    meta = _find_latest_filing(cik, filings_root)
    if not meta:
        return {"cik": cik, "ticker": ticker,
                "skipped": True, "reason": "no local 10-K/10-Q"}

    accession = meta.get("accession_number", "").replace("-", "")
    out_dir = extracted_root / cik / "portfolio" / accession
    portfolio_path = out_dir / "portfolio.json"
    source_path = out_dir / "source.json"

    if portfolio_path.exists() and not force:
        age_h = (time.time() - portfolio_path.stat().st_mtime) / 3600
        if age_h < 20:
            return {"cik": cik, "ticker": ticker,
                    "skipped": True,
                    "reason": f"portfolio fresh ({age_h:.1f}h)",
                    "output_dir": str(out_dir)}

    primary_path = Path(meta["local_primary_path"])
    text = load_primary(primary_path)
    contexts = parse_contexts(text)

    # Two passes of iter_facts would scan the file twice. Build a single
    # list of the concepts we care about up-front.
    wanted_concepts = {"us-gaap:InvestmentOwnedAtFairValue"}
    fv_facts = list(iter_facts(text, concepts=wanted_concepts))

    # Determine the "as-of" period. Most SoIs are instant-dated; pick
    # the latest instant among facts' contexts.
    fv_periods = [contexts[f.context_ref].period_end for f in fv_facts
                  if f.context_ref in contexts and contexts[f.context_ref].period_end]
    period_end = max(fv_periods) if fv_periods else pick_latest_period(contexts.values())

    # Aggregates.
    #
    # Axes are NOT interchangeable for a portfolio total:
    #   * `EquitySecuritiesByIndustryAxis` only covers the unaffiliated
    #     slice (industry is not declared for affiliate-portfolio
    #     companies on most BDCs). So summing it under-counts.
    #   * `InvestmentTypeAxis` has parent/child hierarchy (e.g. filer
    #     declares "First Lien" AND its "First Lien Unitranche" sub-
    #     bucket with overlapping values). Summing double-counts.
    #   * `InvestmentIssuerAffiliationAxis` is a clean partition — every
    #     BDC discloses exactly three members (unaffiliated +
    #     controlled-affiliate + non-controlled-affiliate) and the sum
    #     equals total portfolio FV.
    # So: use affiliation for `total_fv`; use industry for HHI (with an
    # explicit coverage % so downstream consumers know what fraction of
    # the portfolio the HHI is measured over).
    aff_totals = aggregate_fv_by_axis(
        fv_facts, contexts, "us-gaap:InvestmentIssuerAffiliationAxis", period_end)
    total_fv_aff = sum(v for v in aff_totals.values() if v > 0) or None

    # Industry decomposition: try three strategies in priority order.
    #   1) Pure industry-axis FV aggregation (ARCC, GBDC, MFIC, PSEC).
    #   2) Largest-signature FV aggregation (OBDC-style leaves tagging
    #      industry + affiliation + type together).
    #   3) ConcentrationRiskPercentage1 under benchmark=FairValue (MAIN).
    ind_method = None
    ind_totals = aggregate_fv_by_axis(fv_facts, contexts, AXIS_INDUSTRY, period_end)
    if ind_totals:
        ind_method = "pure_axis_fv"
    else:
        leaf_totals, leaf_sig = aggregate_by_axis_best_signature(
            fv_facts, contexts, AXIS_INDUSTRY, period_end)
        if leaf_totals:
            ind_totals = leaf_totals
            ind_method = f"multiaxis_fv:{'+'.join(a.split(':')[-1] for a in leaf_sig)}"
    # FV-derived numbers for coverage/HHI
    total_fv_ind = sum(v for v in ind_totals.values() if v > 0) or None
    total_fv = total_fv_aff or total_fv_ind

    industry_hhi = None
    industry_coverage_pct = None
    industry_shares: dict[str, float] = {}
    if ind_totals:
        industry_hhi = compute_hhi(ind_totals)
        industry_shares = {k: v / total_fv_ind for k, v in ind_totals.items()}
        if total_fv and total_fv_ind:
            industry_coverage_pct = round(total_fv_ind / total_fv * 100, 2)
    else:
        # Percentage fallback (MAIN).
        pct_shares = _industry_shares_from_concentration_percent(
            text, contexts, period_end)
        if pct_shares:
            # Normalize to sum to ~1 (filers occasionally tag in slight
            # rounding error).
            total_share = sum(pct_shares.values()) or 1.0
            industry_shares = {k: v / total_share for k, v in pct_shares.items()}
            industry_hhi = sum(v * v for v in industry_shares.values())
            industry_coverage_pct = round(total_share * 100, 2)
            ind_method = "concentration_pct_fv_benchmark"

    largest_ind = None
    top_industries: list[dict] = []
    if industry_shares:
        # Convert shares back to a pseudo-$ dict so `top_n` can reuse
        # its share math (treats input as absolute values).
        pseudo = {k: v for k, v in industry_shares.items()}
        top_industries = top_n(pseudo, n=5)
        if top_industries:
            largest_ind = top_industries[0]

    # Non-accrual disclosures.
    non_accrual = extract_non_accrual(text, period_end or "", contexts)
    non_accrual_pct_fv = None
    non_accrual_source = None
    if non_accrual.get("pct_fair_value"):
        # Filer tagged it as a percent; may be stored as 0.012 or 1.2.
        val = non_accrual["pct_fair_value"]["value"]
        # Heuristic: anything between 0 and 1 treated as proportion,
        # anything between 1 and 100 treated as percent points.
        if 0 <= val <= 1.0:
            non_accrual_pct_fv = val
        elif 1.0 < val <= 100.0:
            non_accrual_pct_fv = val / 100.0
        non_accrual_source = "direct_tag"

    portfolio = {
        "schema_version": "pricredit.extract_portfolio/v0",
        "cik": cik,
        "ticker": ticker,
        "source_form": meta.get("form"),
        "source_accession": meta.get("accession_number"),
        "source_filing_date": meta.get("filing_date"),
        "period_end": period_end,
        "total_fv": total_fv,
        "total_fv_source_axis": (
            "affiliation" if total_fv_aff else ("industry" if total_fv_ind else None)
        ),
        "n_fv_facts": len(fv_facts),
        "n_industries": len(industry_shares),
        "industry_coverage_pct": industry_coverage_pct,
        "industry_hhi": round(industry_hhi, 6) if industry_hhi else None,
        "industry_hhi_effective_n": round(1.0 / industry_hhi, 2) if industry_hhi else None,
        "industry_method": ind_method,
        "largest_industry": largest_ind,
        "top_industries": top_industries,
        "affiliation_mix": {
            _clean_member_label(k): round(v / total_fv_aff, 4)
            for k, v in (aff_totals or {}).items()
        } if total_fv_aff else {},
        "non_accrual_pct_fair_value": non_accrual_pct_fv,
        "non_accrual_source": non_accrual_source,
        "non_accrual_pct_cost": (
            (non_accrual["pct_cost"] or {}).get("value") if non_accrual.get("pct_cost") else None
        ),
        "non_accrual_count": (
            (non_accrual["count"] or {}).get("value") if non_accrual.get("count") else None
        ),
        "disclaimer": (
            "Portfolio aggregates derived from the BDC's own iXBRL facts "
            "on the latest 10-K / 10-Q Schedule of Investments. Non-accrual "
            "percent is only populated when the filer tags the concept "
            "directly; otherwise it is null."
        ),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    portfolio_path.write_text(
        json.dumps(portfolio, indent=2, ensure_ascii=False), encoding="utf-8")
    source_path.write_text(json.dumps({
        "accession_number": meta.get("accession_number"),
        "form": meta.get("form"),
        "filing_date": meta.get("filing_date"),
        "report_date": meta.get("report_date"),
        "primary_document": meta.get("primary_document"),
        "size_bytes": meta.get("size"),
        "primary_url": meta.get("primary_url"),
        "parsed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Merge a compact block into facts/summary.json so compute_risk.py
    # can consume it without touching the raw iXBRL.
    facts_summary = extracted_root / cik / "facts" / "summary.json"
    if facts_summary.exists():
        try:
            s = json.loads(facts_summary.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        s["portfolio"] = {
            "source_form": portfolio["source_form"],
            "source_accession": portfolio["source_accession"],
            "period_end": portfolio["period_end"],
            "total_fv": portfolio["total_fv"],
            "total_fv_source_axis": portfolio["total_fv_source_axis"],
            "n_industries": portfolio["n_industries"],
            "industry_coverage_pct": portfolio["industry_coverage_pct"],
            "industry_hhi": portfolio["industry_hhi"],
            "largest_industry_pct": (
                (portfolio["largest_industry"] or {}).get("pct_portfolio")
            ),
            "non_accrual_pct_fair_value": portfolio["non_accrual_pct_fair_value"],
            "non_accrual_source": portfolio["non_accrual_source"],
        }
        facts_summary.write_text(
            json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "cik": cik, "ticker": ticker,
        "accession": meta.get("accession_number"),
        "form": meta.get("form"),
        "period_end": period_end,
        "total_fv": total_fv,
        "n_industries": len(ind_totals),
        "industry_hhi": portfolio["industry_hhi"],
        "non_accrual_pct_fair_value": non_accrual_pct_fv,
        "output_dir": str(out_dir),
    }


# ---------------------------------------------------------------------------
# CLI driver.
# ---------------------------------------------------------------------------

def _print_portfolio(ticker: str, path: Path) -> None:
    try:
        p = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    print(f"\n=== {ticker} portfolio ({p.get('source_form')} "
          f"{p.get('source_accession')} — {p.get('period_end')}) ===",
          file=sys.stderr)
    tfv = p.get("total_fv")
    if tfv:
        print(f"  Total FV:              ${tfv/1e9:>6.2f}B "
              f"(from {p.get('total_fv_source_axis')} axis)",
              file=sys.stderr)
    mix = p.get("affiliation_mix") or {}
    if mix:
        # "Investment Unaffiliated Issuer" -> "Unaffiliated"
        # "Investment Affiliated Issuer Noncontrolled" -> "Affiliated-Noncontrolled"
        def _label(s: str) -> str:
            s = s.replace("Investment", "").replace("Issuer", "").strip()
            parts = s.split()
            if not parts:
                return "Other"
            head = parts[0]
            tail = "".join(parts[1:])
            return f"{head}-{tail}" if tail else head
        mix_str = ", ".join(f"{_label(k)}={v*100:.1f}%" for k, v in mix.items())
        print(f"  Affiliation split:    {mix_str}", file=sys.stderr)
    hhi = p.get("industry_hhi")
    if hhi is not None:
        cov = p.get("industry_coverage_pct")
        print(f"  Industry HHI:          {hhi:.4f}  "
              f"(effective N = {p.get('industry_hhi_effective_n')}; "
              f"covers {cov}% of portfolio)",
              file=sys.stderr)
    top = p.get("top_industries") or []
    for t in top[:5]:
        print(f"    {t['pct_portfolio']:>6.2f}%  {t['name']}", file=sys.stderr)
    nap = p.get("non_accrual_pct_fair_value")
    if nap is not None:
        print(f"  Non-accrual %FV:      {nap*100:.2f}%  "
              f"(source: {p.get('non_accrual_source')})", file=sys.stderr)
    else:
        print(f"  Non-accrual %FV:      n/a (filer does not tag concept)",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    ap.add_argument("--filings", default=str(DEFAULT_FILINGS))
    ap.add_argument("--out", default=str(DEFAULT_EXTRACTED))
    ap.add_argument("--ticker", default="")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--ciks", default="")
    ap.add_argument("--max-bdcs", type=int, default=0)
    ap.add_argument("--force", action="store_true",
                    help="Re-parse even if portfolio.json is fresh.")
    ap.add_argument("--public-only", action="store_true", default=True)
    ap.add_argument("--include-private", dest="public_only", action="store_false")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="Print per-BDC summary to stderr.")
    ap.add_argument("--run-summary", default="",
                    help="Optional path for a run-level summary JSON.")
    args = ap.parse_args()

    univ_path = Path(args.universe)
    if not univ_path.exists():
        print(f"[portfolio] universe not found: {univ_path}", file=sys.stderr)
        return 2
    universe = json.loads(univ_path.read_text(encoding="utf-8"))
    bdcs: list[dict] = universe.get("bdcs") or []
    if args.public_only:
        bdcs = [b for b in bdcs if b.get("publicly_traded")]

    tickers: list[str] = []
    if args.ticker:
        tickers.append(args.ticker)
    if args.tickers:
        tickers.extend(t.strip() for t in args.tickers.split(","))
    tickers = [t.strip().upper() for t in tickers if t.strip()]
    if tickers:
        wanted = set(tickers)
        bdcs = [b for b in bdcs if
                (b.get("primary_ticker") or "").upper() in wanted
                or any((t or "").upper() in wanted for t in (b.get("tickers") or []))]
    if args.ciks.strip():
        wanted_c = {c.strip().zfill(10) for c in args.ciks.split(",") if c.strip()}
        bdcs = [b for b in bdcs if b.get("cik") in wanted_c]
    if args.max_bdcs:
        bdcs = bdcs[:args.max_bdcs]

    if not bdcs:
        print("[portfolio] no BDCs match filters.", file=sys.stderr)
        return 0

    print(f"[portfolio] extracting SoI aggregates for {len(bdcs)} BDCs"
          f" (force={args.force})", file=sys.stderr)

    filings_root = Path(args.filings)
    extracted_root = Path(args.out)

    results: list[dict] = []
    for b in bdcs:
        try:
            r = extract_one_bdc(b, filings_root, extracted_root, force=args.force)
        except Exception as exc:  # pragma: no cover
            r = {"cik": b.get("cik"),
                 "ticker": b.get("primary_ticker"),
                 "error": repr(exc)}
        results.append(r)
        tkr = (r.get("ticker") or r.get("cik") or "?")
        if r.get("skipped"):
            print(f"[portfolio]   {tkr}: skip ({r.get('reason')})", file=sys.stderr)
        elif r.get("error"):
            print(f"[portfolio]   {tkr}: ERROR {r.get('error')}", file=sys.stderr)
        else:
            hhi = r.get("industry_hhi")
            nap = r.get("non_accrual_pct_fair_value")
            print(f"[portfolio]   {tkr}: "
                  f"form={r.get('form')} "
                  f"as_of={r.get('period_end')} "
                  f"hhi={hhi:.3f} " if hhi is not None else
                  f"[portfolio]   {tkr}: "
                  f"form={r.get('form')} "
                  f"as_of={r.get('period_end')} "
                  f"hhi=n/a ",
                  file=sys.stderr, end="")
            print(f"nacc={(nap*100):.2f}%" if nap is not None else "nacc=n/a",
                  file=sys.stderr)
            if args.do_print:
                _print_portfolio(tkr,
                    Path(r["output_dir"]) / "portfolio.json")

    summary = {
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_bdcs": len(bdcs),
        "n_extracted": sum(1 for r in results if not r.get("skipped") and not r.get("error")),
        "n_skipped": sum(1 for r in results if r.get("skipped")),
        "n_errors": sum(1 for r in results if r.get("error")),
        "n_with_hhi": sum(1 for r in results if r.get("industry_hhi") is not None),
        "n_with_non_accrual": sum(1 for r in results
                                  if r.get("non_accrual_pct_fair_value") is not None),
        "results": results,
    }
    if args.run_summary:
        p = Path(args.run_summary)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[portfolio] run summary -> {p}", file=sys.stderr)
    print(f"[portfolio] done: extracted={summary['n_extracted']}"
          f" skipped={summary['n_skipped']}"
          f" errors={summary['n_errors']}"
          f" with_hhi={summary['n_with_hhi']}"
          f" with_non_accrual={summary['n_with_non_accrual']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
