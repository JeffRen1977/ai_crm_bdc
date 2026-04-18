#!/usr/bin/env python3
"""
Parse EDGAR XBRL company facts into PriCredit's canonical metrics.

For each BDC in bdc/bdc_universe.json (or filtered subset) we:
  1. Pull companyfacts/CIK{cik}.json (24h-cached, throttled).
  2. Resolve the canonical metrics defined in _xbrl_concepts.CONCEPT_MAP
     by walking the taxonomies in priority order; fall back to regex
     patterns across all taxonomies if needed.
  3. Build a time series per metric, deduplicating by (period_end,
     period_start) and keeping the most recently `filed` observation
     (handles 10-K/A restatements cleanly).
  4. Compute QoQ / YoY percent changes on NAV per share and compute
     the derived ratios (leverage, fair-vs-cost, PIK ratio, dividend
     coverage, NII per share).
  5. Emit:
       extracted/<cik>/facts/timeseries.json
       extracted/<cik>/facts/latest.json
       extracted/<cik>/facts/resolved.json
       extracted/<cik>/facts/summary.json
  6. Write a parse summary for the run under reports/<DATE>/.

Examples:
    scripts/parse_filings.py --ticker ARCC --print-latest
    scripts/parse_filings.py --tickers ARCC,MAIN,OBDC
    scripts/parse_filings.py                 # all publicly_traded BDCs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edgar_common import (  # noqa: E402
    companyfacts_url,
    edgar_get_json,
    pad_cik,
    preflight,
)
from _xbrl_concepts import (  # noqa: E402
    CONCEPT_MAP,
    CONCEPT_PATTERNS,
    DERIVATIONS,
    METRIC_KIND,
    find_prior,
    pct_change,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"
DEFAULT_OUT = ROOT / "extracted"


# ---------------------------------------------------------------------------
# Resolution: pick the best XBRL tag per canonical metric.
# ---------------------------------------------------------------------------

def _observations(facts: dict, tax: str, tag: str, unit: str) -> list[dict]:
    try:
        obs = facts["facts"][tax][tag]["units"][unit]
    except KeyError:
        return []
    return obs or []


def resolve_concept(facts: dict, metric: str) -> dict | None:
    """Return {'taxonomy','tag','unit','observations'} for `metric`, or None."""
    # 1. Priority-ordered explicit map.
    for tax, tag, unit in CONCEPT_MAP.get(metric, []):
        obs = _observations(facts, tax, tag, unit)
        if obs:
            return {"taxonomy": tax, "tag": tag, "unit": unit,
                    "observations": obs, "source": "explicit"}
    # 2. Regex fallback across all taxonomies.
    for fb_metric, pattern, unit in CONCEPT_PATTERNS:
        if fb_metric != metric:
            continue
        for tax, concepts in (facts.get("facts") or {}).items():
            for tag in concepts:
                if not pattern.search(tag):
                    continue
                obs = _observations(facts, tax, tag, unit)
                if obs:
                    return {"taxonomy": tax, "tag": tag, "unit": unit,
                            "observations": obs, "source": "pattern"}
    return None


# ---------------------------------------------------------------------------
# Time-series construction with restatement-aware dedup.
# ---------------------------------------------------------------------------

def build_timeseries(resolved: dict[str, dict]) -> dict[str, list[dict]]:
    """For each metric, dedup observations by (start,end), keeping the
    most recently `filed` record. Drops quarterly duplicates in favor
    of 10-K/A amendments automatically."""
    out: dict[str, list[dict]] = {}
    for metric, info in resolved.items():
        if info is None:
            continue
        kind = METRIC_KIND.get(metric, "instant")
        by_period: dict[tuple, dict] = {}
        for ob in info["observations"]:
            end = ob.get("end") or ""
            start = ob.get("start") or ""
            key = (start, end) if kind == "duration" else (end,)
            filed = ob.get("filed") or ""
            kept = by_period.get(key)
            if kept is None or (ob.get("filed") or "") > (kept.get("filed") or ""):
                by_period[key] = ob
        series = sorted(by_period.values(), key=lambda o: (o.get("end") or ""))
        out[metric] = [{
            "end": o.get("end"),
            "start": o.get("start"),
            "val": o.get("val"),
            "fy": o.get("fy"),
            "fp": o.get("fp"),
            "form": o.get("form"),
            "accn": o.get("accn"),
            "filed": o.get("filed"),
        } for o in series]
    return out


def latest_per_metric(timeseries: dict[str, list[dict]]) -> dict[str, dict]:
    """Latest observation per metric (by end date)."""
    latest: dict[str, dict] = {}
    for metric, series in timeseries.items():
        if not series:
            continue
        latest[metric] = series[-1]
    return latest


# ---------------------------------------------------------------------------
# Summary: latest snapshot + derivations + NAV trend.
# ---------------------------------------------------------------------------

def build_summary(cik: str, bdc_meta: dict,
                  timeseries: dict[str, list[dict]],
                  latest: dict[str, dict],
                  resolved: dict[str, dict]) -> dict:
    derived: list[dict] = []
    for fn in DERIVATIONS:
        try:
            d = fn(latest)
        except Exception as exc:  # pragma: no cover
            d = {"name": fn.__name__, "error": repr(exc)}
        if d:
            derived.append(d)

    nav_series = timeseries.get("nav_per_share") or []
    nav_latest = nav_series[-1] if nav_series else None
    nav_qoq = nav_yoy = None
    if nav_latest:
        ref_end = nav_latest["end"]
        ref_val = float(nav_latest["val"])
        q_prior = find_prior(nav_series, ref_end, months_ago=3)
        y_prior = find_prior(nav_series, ref_end, months_ago=12)
        if q_prior:
            nav_qoq = {
                "from_end": q_prior["end"],
                "to_end": ref_end,
                "pct_change": pct_change(ref_val, float(q_prior["val"])),
            }
        if y_prior:
            nav_yoy = {
                "from_end": y_prior["end"],
                "to_end": ref_end,
                "pct_change": pct_change(ref_val, float(y_prior["val"])),
            }

    return {
        "schema_version": "pricredit.parse_filings/v0",
        "cik": pad_cik(cik),
        "ticker": bdc_meta.get("primary_ticker"),
        "company_name": bdc_meta.get("company_name"),
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latest": latest,
        "derived": derived,
        "nav_trend": {"qoq": nav_qoq, "yoy": nav_yoy},
        "coverage": {
            "resolved_metrics": sorted([m for m, v in resolved.items() if v]),
            "unresolved_metrics": sorted([m for m in CONCEPT_MAP
                                          if m not in resolved or resolved[m] is None]),
        },
        "disclaimer": (
            "Derived from publicly filed XBRL facts on SEC EDGAR. "
            "Informational only; not investment advice. "
            "Values are as-reported by the BDC and subject to later restatement."
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration / IO.
# ---------------------------------------------------------------------------

def process_bdc(bdc: dict, out_root: Path, force: bool,
                print_latest: bool) -> dict:
    cik = bdc["cik"]
    tag = bdc.get("primary_ticker") or bdc.get("company_name") or cik
    ticker_dir = out_root / cik / "facts"
    summary_path = ticker_dir / "summary.json"

    if summary_path.exists() and not force:
        age_h = (time.time() - summary_path.stat().st_mtime) / 3600
        if age_h < 20:  # skip if parsed in the last ~day
            return {"cik": cik, "ticker": tag, "skipped": True,
                    "reason": f"summary is fresh ({age_h:.1f}h)"}

    try:
        facts = edgar_get_json(companyfacts_url(cik), cache_ttl_s=24 * 60 * 60)
    except Exception as exc:
        return {"cik": cik, "ticker": tag, "error": f"companyfacts: {exc}"}

    resolved: dict[str, dict] = {}
    for metric in CONCEPT_MAP:
        resolved[metric] = resolve_concept(facts, metric)

    timeseries = build_timeseries(resolved)
    latest = latest_per_metric(timeseries)
    summary = build_summary(cik, bdc, timeseries, latest, resolved)

    ticker_dir.mkdir(parents=True, exist_ok=True)
    (ticker_dir / "timeseries.json").write_text(
        json.dumps(timeseries, indent=2, ensure_ascii=False), encoding="utf-8")
    (ticker_dir / "latest.json").write_text(
        json.dumps(latest, indent=2, ensure_ascii=False), encoding="utf-8")
    (ticker_dir / "resolved.json").write_text(
        json.dumps({k: (None if v is None else {
            "taxonomy": v["taxonomy"], "tag": v["tag"], "unit": v["unit"],
            "source": v["source"], "n_observations": len(v["observations"]),
        }) for k, v in resolved.items()},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if print_latest:
        _print_latest(tag, summary)

    return {
        "cik": cik, "ticker": tag,
        "resolved_count": sum(1 for v in resolved.values() if v),
        "unresolved": sorted([m for m, v in resolved.items() if v is None]),
        "nav_end": (latest.get("nav_per_share") or {}).get("end"),
        "nav_val": (latest.get("nav_per_share") or {}).get("val"),
        "output_dir": str(ticker_dir),
    }


def _print_latest(tag: str, summary: dict) -> None:
    print(f"\n=== {tag}  (as of {summary.get('as_of_utc')}) ===",
          file=sys.stderr)
    latest = summary.get("latest") or {}
    for m in [
        "nav_per_share", "net_assets", "total_assets", "total_liabilities",
        "total_debt", "investments_fair_value", "investments_cost",
        "asset_coverage_ratio", "shares_outstanding", "net_investment_income",
        "total_investment_income", "interest_income_pik", "dividend_income_pik",
        "distributions_per_share",
    ]:
        v = latest.get(m)
        if not v:
            continue
        rng = f"{v.get('start') or ''}..{v.get('end')}" if v.get('start') else v.get('end')
        print(f"  {m:28s} {str(v.get('val')):>20} [{v.get('form')} {rng}]",
              file=sys.stderr)
    print("  --- derived ---", file=sys.stderr)
    for d in summary.get("derived") or []:
        val = d.get("val")
        if val is None:
            continue
        print(f"  {d['name']:28s} {val:>20.4f}", file=sys.stderr)
    trend = summary.get("nav_trend") or {}
    for k in ("qoq", "yoy"):
        t = trend.get(k)
        if t and t.get("pct_change") is not None:
            print(f"  nav_{k:<25} {t['pct_change']*100:>19.2f}%"
                  f"  [{t['from_end']} -> {t['to_end']}]", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--ticker", default="",
                    help="Single ticker to parse (shorthand for --tickers X).")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated tickers to restrict to.")
    ap.add_argument("--ciks", default="",
                    help="Comma-separated CIKs to restrict to.")
    ap.add_argument("--max-bdcs", type=int, default=0,
                    help="Cap BDCs processed this run (0 = all).")
    ap.add_argument("--force", action="store_true",
                    help="Re-parse even if summary.json is fresh.")
    ap.add_argument("--print-latest", action="store_true",
                    help="Print per-BDC snapshot to stderr.")
    ap.add_argument("--public-only", action="store_true", default=True,
                    help="Skip BDCs flagged publicly_traded=false (default).")
    ap.add_argument("--include-private", dest="public_only",
                    action="store_false")
    ap.add_argument("--run-summary", default="",
                    help="Optional path to write a global parse summary JSON.")
    args = ap.parse_args()

    preflight()

    univ_path = Path(args.universe)
    if not univ_path.exists():
        print(f"[parse] universe not found: {univ_path}\n"
              f"        Run scripts/discover_bdcs.py first.",
              file=sys.stderr)
        return 2

    universe = json.loads(univ_path.read_text(encoding="utf-8"))
    bdcs: list[dict] = universe.get("bdcs") or []
    if args.public_only:
        bdcs = [b for b in bdcs if b.get("publicly_traded")]

    tickers = []
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
        wanted_c = {pad_cik(c.strip()) for c in args.ciks.split(",") if c.strip()}
        bdcs = [b for b in bdcs if b.get("cik") in wanted_c]
    if args.max_bdcs:
        bdcs = bdcs[:args.max_bdcs]

    if not bdcs:
        print("[parse] no BDCs match filters; nothing to do.", file=sys.stderr)
        return 0

    print(f"[parse] parsing XBRL facts for {len(bdcs)} BDCs"
          f" (public_only={args.public_only}, force={args.force})",
          file=sys.stderr)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for b in bdcs:
        try:
            res = process_bdc(b, out_root, args.force, args.print_latest)
        except Exception as exc:  # pragma: no cover
            res = {"cik": b.get("cik"), "ticker": b.get("primary_ticker"),
                   "error": repr(exc)}
        results.append(res)
        if res.get("skipped"):
            print(f"[parse]   {res.get('ticker')}: skip ({res.get('reason')})",
                  file=sys.stderr)
        elif res.get("error"):
            print(f"[parse]   {res.get('ticker')}: ERROR {res.get('error')}",
                  file=sys.stderr)
        else:
            print(f"[parse]   {res.get('ticker')}: "
                  f"resolved={res.get('resolved_count')} "
                  f"nav={res.get('nav_val')} as_of={res.get('nav_end')}",
                  file=sys.stderr)

    # Run summary.
    summary = {
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_bdcs_attempted": len(bdcs),
        "n_skipped": sum(1 for r in results if r.get("skipped")),
        "n_errors": sum(1 for r in results if r.get("error")),
        "n_parsed": sum(1 for r in results if not r.get("skipped") and not r.get("error")),
        "coverage_by_metric": _coverage_by_metric(results),
        "results": results,
    }
    if args.run_summary:
        Path(args.run_summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.run_summary).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[parse] run summary -> {args.run_summary}", file=sys.stderr)
    print(f"[parse] done: parsed={summary['n_parsed']}"
          f" skipped={summary['n_skipped']}"
          f" errors={summary['n_errors']}", file=sys.stderr)
    return 0


def _coverage_by_metric(results: list[dict]) -> dict[str, int]:
    """Count how many BDCs had each canonical metric unresolved."""
    metric_miss: dict[str, int] = {m: 0 for m in CONCEPT_MAP}
    denom = 0
    for r in results:
        if r.get("skipped") or r.get("error"):
            continue
        denom += 1
        for m in r.get("unresolved") or []:
            metric_miss[m] = metric_miss.get(m, 0) + 1
    return {"bdcs_counted": denom, "unresolved_counts": metric_miss}


if __name__ == "__main__":
    raise SystemExit(main())
