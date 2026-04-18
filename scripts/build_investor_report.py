#!/usr/bin/env python3
"""
PriCredit / AI-CRM — investor brief generator (v0).

For each scored BDC, compose a per-BDC memo from the already-produced
artifacts:
  * extracted/<cik>/facts/summary.json        (canonical XBRL metrics)
  * extracted/<cik>/facts/timeseries.json     (NAV history)
  * extracted/<cik>/portfolio/<accn>/portfolio.json (SoI aggregates)
  * reports/<DATE>/risk_<TICKER>.json         (factor audit + composite)
  * reports/<DATE>/alert_*.json               (open alerts for that BDC)

Outputs land under reports/<DATE>/briefs/:
  <TICKER>.md     human-readable investor brief
  <TICKER>.json   machine-readable brief (same content, normalized)
  index.md        one-page roll-up sorted by composite score
  index.json      machine-readable roll-up

The brief is markdown-first so it renders cleanly in GitHub / Cursor /
email clients / static site generators. JSON mirrors the structure
for downstream consumers (CRM, dashboards).

Examples:
    scripts/build_investor_report.py --date 2026-04-18-final
    scripts/build_investor_report.py --tickers ARCC,MAIN,OBDC --print
    scripts/build_investor_report.py --date 2026-04-18 --force
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
from _edgar_common import pad_cik  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXTRACTED = ROOT / "extracted"
DEFAULT_REPORTS = ROOT / "reports"
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"

SCHEMA_VERSION = "pricredit.investor_brief/v0"


# ---------------------------------------------------------------------------
# Formatting helpers.
# ---------------------------------------------------------------------------

def fmt_money(x: float | int | None, unit: str = "$") -> str:
    """Human-friendly compact money formatting ($31.2B, $480.5M, $12.3K)."""
    if x is None:
        return "n/a"
    try:
        v = float(x)
    except Exception:
        return "n/a"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return f"{sign}{unit}{a/1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}{unit}{a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}{unit}{a/1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}{unit}{a/1e3:.1f}K"
    return f"{sign}{unit}{a:.2f}"


def fmt_pct(x: float | None, digits: int = 2, already_pct: bool = False) -> str:
    if x is None:
        return "n/a"
    try:
        v = float(x)
    except Exception:
        return "n/a"
    if already_pct:
        return f"{v:.{digits}f}%"
    return f"{v*100:.{digits}f}%"


def fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "n/a"


def fmt_date(s: str | None) -> str:
    return s if s else "n/a"


def arrow_for_delta(pct: float | None, good_when_positive: bool = True) -> str:
    """Return an ASCII arrow summarizing direction. No emoji."""
    if pct is None:
        return "—"
    if pct > 0.0005:
        return "up" if good_when_positive else "up*"
    if pct < -0.0005:
        return "down*" if good_when_positive else "down"
    return "flat"


def sparkline(values: list[float]) -> str:
    """Unicode block sparkline from numeric values (stable across BDCs)."""
    if not values:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return bars[3] * len(values)
    rng = hi - lo
    out = []
    for v in values:
        idx = int(round((v - lo) / rng * (len(bars) - 1)))
        out.append(bars[max(0, min(len(bars) - 1, idx))])
    return "".join(out)


_SEVERITY_EMOJI = {
    "critical": "[CRITICAL]",
    "high":     "[HIGH]",
    "medium":   "[MEDIUM]",
    "low":      "[LOW]",
    "info":     "[INFO]",
}


# ---------------------------------------------------------------------------
# Data loaders.
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_portfolio(cik: str, extracted_root: Path) -> dict | None:
    """Prefer the portfolio.json that matches summary.portfolio.source_accession
    if available, otherwise fall back to the newest-on-disk."""
    pdir = extracted_root / cik / "portfolio"
    if not pdir.exists():
        return None
    candidates = sorted(pdir.glob("*/portfolio.json"))
    if not candidates:
        return None
    return _load_json(candidates[-1])


def _alerts_for_ticker(reports_date_dir: Path, ticker: str) -> list[dict]:
    alerts: list[dict] = []
    prefix = f"alert_RISK-{ticker}-"
    for fp in sorted(reports_date_dir.glob("alert_RISK-*.json")):
        if not fp.name.startswith(prefix):
            continue
        doc = _load_json(fp)
        if doc:
            alerts.append(doc)
    _rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    alerts.sort(key=lambda a: _rank.get(a.get("severity") or "", -1), reverse=True)
    return alerts


def _derived(summary: dict, name: str) -> float | None:
    for d in summary.get("derived") or []:
        if d.get("name") == name:
            try:
                return float(d.get("val"))
            except Exception:
                return None
    return None


def _latest(summary: dict, metric: str) -> dict | None:
    return (summary.get("latest") or {}).get(metric)


def _latest_val(summary: dict, metric: str) -> float | None:
    obs = _latest(summary, metric)
    if not obs:
        return None
    try:
        return float(obs.get("val"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Brief assembly.
# ---------------------------------------------------------------------------

def build_brief(summary: dict, risk: dict, alerts: list[dict],
                portfolio: dict | None, timeseries: dict | None) -> dict:
    """Normalize all inputs into a single dict used to render both
    markdown and JSON brief outputs."""

    nav_ts: list[dict] = []
    if timeseries:
        pts = timeseries.get("nav_per_share") or []
        nav_ts = [p for p in pts if p.get("val") is not None][-8:]

    latest = summary.get("latest") or {}
    nav_obs = latest.get("nav_per_share") or {}
    filing_end = nav_obs.get("end") or risk.get("as_of_filing_end")

    ds = _latest(summary, "distributions_per_share") or {}
    dps = ds.get("val") if isinstance(ds.get("val"), (int, float)) else None
    nii_per_share = _derived(summary, "nii_per_share")
    div_coverage = _derived(summary, "dividend_coverage_nii_over_divs")
    pik_ratio = _derived(summary, "pik_income_ratio")
    lev_de = _derived(summary, "leverage_debt_to_equity")
    fv_cost = _derived(summary, "fair_value_to_cost")

    nav_trend = summary.get("nav_trend") or {}
    qoq = (nav_trend.get("qoq") or {}).get("pct_change")
    yoy = (nav_trend.get("yoy") or {}).get("pct_change")

    factor_contribs = [f for f in (risk.get("factors") or [])
                       if not f.get("excluded")]
    factor_contribs.sort(key=lambda f: abs(f.get("contribution", 0)), reverse=True)
    top_factors = factor_contribs[:3]

    portfolio_block = summary.get("portfolio") or {}
    top_industries = (portfolio or {}).get("top_industries") or []
    affiliation_mix = (portfolio or {}).get("affiliation_mix") or {}
    ind_method = (portfolio or {}).get("industry_method")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_date": risk.get("run_date"),
        "ticker": risk.get("ticker") or summary.get("ticker"),
        "cik": risk.get("cik") or summary.get("cik"),
        "company_name": risk.get("company_name") or summary.get("company_name"),
        "as_of_filing_end": filing_end,
        "filing": {
            "form": nav_obs.get("form") or portfolio_block.get("source_form"),
            "accession": nav_obs.get("accn") or portfolio_block.get("source_accession"),
            "filed": nav_obs.get("filed"),
        },
        "risk": {
            "composite_score": risk.get("composite_score"),
            "band": risk.get("band"),
            "n_factors_used": risk.get("n_factors_used"),
            "n_factors_missing": risk.get("n_factors_missing"),
            "top_contributors": [
                {
                    "name": f["name"],
                    "raw_value": f.get("raw_value"),
                    "score": f.get("score"),
                    "weight": f.get("weight"),
                    "contribution": f.get("contribution"),
                    "direction": f.get("direction"),
                    "used_fallback": f.get("used_fallback", False),
                }
                for f in top_factors
            ],
            "factors": risk.get("factors") or [],
        },
        "alerts": [
            {
                "alert_id": a.get("alert_id"),
                "reason": a.get("alert_reason"),
                "severity": a.get("severity"),
                "description": a.get("description"),
                "triggers": a.get("triggers") or [],
            }
            for a in alerts
        ],
        "financial_snapshot": {
            "nav_per_share": _latest_val(summary, "nav_per_share"),
            "shares_outstanding": _latest_val(summary, "shares_outstanding"),
            "net_assets": _latest_val(summary, "net_assets"),
            "total_assets": _latest_val(summary, "total_assets"),
            "total_debt": _latest_val(summary, "total_debt"),
            "investments_fair_value": _latest_val(summary, "investments_fair_value"),
            "investments_cost": _latest_val(summary, "investments_cost"),
            "asset_coverage_ratio": _latest_val(summary, "asset_coverage_ratio"),
            "leverage_debt_to_equity": lev_de,
            "fair_value_to_cost": fv_cost,
            "net_investment_income": _latest_val(summary, "net_investment_income"),
            "total_investment_income": _latest_val(summary, "total_investment_income"),
            "distributions_per_share": dps,
            "dividends_paid": _latest_val(summary, "dividends_paid"),
            "nii_per_share": nii_per_share,
            "dividend_coverage_nii_over_divs": div_coverage,
            "pik_income_ratio": pik_ratio,
            "period_start": (_latest(summary, "net_investment_income") or {}).get("start"),
            "period_end": (_latest(summary, "net_investment_income") or {}).get("end"),
        },
        "nav_trend": {
            "qoq_pct_change": qoq,
            "yoy_pct_change": yoy,
            "history": [
                {"end": p.get("end"), "val": p.get("val"), "form": p.get("form")}
                for p in nav_ts
            ],
        },
        "portfolio": {
            "as_of": portfolio_block.get("period_end"),
            "source_form": portfolio_block.get("source_form"),
            "source_accession": portfolio_block.get("source_accession"),
            "total_fair_value": portfolio_block.get("total_fv"),
            "total_fv_source_axis": portfolio_block.get("total_fv_source_axis"),
            "n_industries": portfolio_block.get("n_industries"),
            "industry_hhi": portfolio_block.get("industry_hhi"),
            "industry_hhi_effective_n":
                (portfolio or {}).get("industry_hhi_effective_n"),
            "industry_coverage_pct": portfolio_block.get("industry_coverage_pct"),
            "industry_method": ind_method,
            "largest_industry_pct": portfolio_block.get("largest_industry_pct"),
            "top_industries": top_industries[:5],
            "affiliation_mix": affiliation_mix,
            "non_accrual_pct_fair_value":
                portfolio_block.get("non_accrual_pct_fair_value"),
            "non_accrual_source": portfolio_block.get("non_accrual_source"),
        } if (portfolio_block or portfolio) else None,
        "disclaimer": (
            "Derived from publicly filed SEC EDGAR data. Informational "
            "only; not investment advice. Values are as-reported and "
            "subject to later restatement. Risk scoring is heuristic "
            "(v1 weights)."
        ),
    }


# ---------------------------------------------------------------------------
# Markdown renderer.
# ---------------------------------------------------------------------------

_BAND_HEADLINE = {
    "low":      "solid credit profile",
    "medium":   "watch-list",
    "high":     "elevated risk",
    "critical": "critical risk",
    "unknown":  "insufficient data",
}


def _headline(brief: dict) -> str:
    band = (brief["risk"]["band"] or "unknown").lower()
    score = brief["risk"]["composite_score"]
    tag = _BAND_HEADLINE.get(band, band)
    if score is None:
        return f"{brief['ticker']} — {tag}"
    return f"{brief['ticker']} — {tag} (score {score:.1f}/100, {band.upper()})"


def render_markdown(brief: dict) -> str:
    r = brief["risk"]
    fs = brief["financial_snapshot"]
    nt = brief["nav_trend"]
    p = brief.get("portfolio") or {}
    alerts = brief.get("alerts") or []

    lines: list[str] = []

    lines.append(f"# {_headline(brief)}")
    lines.append("")

    # --- Header table ------------------------------------------------------
    filing = brief.get("filing") or {}
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Company | {brief.get('company_name') or 'n/a'} |")
    lines.append(f"| CIK | {brief.get('cik') or 'n/a'} |")
    lines.append(f"| Filing as-of | {fmt_date(brief.get('as_of_filing_end'))} "
                 f"({filing.get('form') or 'n/a'}, accession "
                 f"{filing.get('accession') or 'n/a'}, filed "
                 f"{fmt_date(filing.get('filed'))}) |")
    lines.append(f"| Brief generated | {brief.get('generated_at_utc')} "
                 f"(run {brief.get('run_date') or 'n/a'}) |")
    lines.append("")

    # --- Risk snapshot -----------------------------------------------------
    lines.append("## Risk snapshot")
    lines.append("")
    if r.get("composite_score") is None:
        lines.append("_Risk score unavailable (no factors resolved)._")
    else:
        lines.append(f"- **Composite score:** {r['composite_score']:.1f} / 100 "
                     f"({(r.get('band') or 'unknown').upper()})")
        lines.append(f"- **Factors used:** {r.get('n_factors_used')} of "
                     f"{int(r.get('n_factors_used') or 0) + int(r.get('n_factors_missing') or 0)}"
                     f" ({r.get('n_factors_missing') or 0} missing)")
        tops = r.get("top_contributors") or []
        if tops:
            lines.append("- **Largest contributors:** "
                         + ", ".join(
                             f"{t['name']} ({(t.get('contribution') or 0):+.1f} pts, "
                             f"raw={fmt_num(t.get('raw_value'), 4)})"
                             for t in tops))
    lines.append("")

    # --- Open alerts -------------------------------------------------------
    lines.append("## Open alerts")
    lines.append("")
    if not alerts:
        lines.append("_No alerts firing._")
    else:
        for a in alerts:
            sev = (a.get("severity") or "").lower()
            tag = _SEVERITY_EMOJI.get(sev, f"[{sev.upper() or 'ALERT'}]")
            lines.append(f"- {tag} **{a.get('reason')}** — {a.get('description') or ''}")
            for t in a.get("triggers") or []:
                src = t.get("source")
                val = t.get("value")
                op = t.get("op")
                thr = t.get("threshold")
                lines.append(f"    - `{src}` = {fmt_num(val, 4)} {op} {thr}")
    lines.append("")

    # --- Financial snapshot ------------------------------------------------
    lines.append("## Financial snapshot (latest filed period)")
    lines.append("")
    period_str = ""
    if fs.get("period_start") and fs.get("period_end"):
        period_str = f" ({fs['period_start']} → {fs['period_end']})"
    lines.append(f"_Flow metrics cover{period_str}._")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| NAV / share | {fmt_money(fs.get('nav_per_share'))} |")
    lines.append(f"| Net assets | {fmt_money(fs.get('net_assets'))} |")
    lines.append(f"| Total assets | {fmt_money(fs.get('total_assets'))} |")
    lines.append(f"| Total debt | {fmt_money(fs.get('total_debt'))} |")
    lines.append(f"| Asset coverage ratio | {fmt_num(fs.get('asset_coverage_ratio'))} |")
    lines.append(f"| Leverage (Debt / Equity) | {fmt_num(fs.get('leverage_debt_to_equity'))} |")
    lines.append(f"| Investments @ fair value | {fmt_money(fs.get('investments_fair_value'))} |")
    lines.append(f"| Fair value / cost | {fmt_num(fs.get('fair_value_to_cost'), 4)} |")
    lines.append(f"| Net investment income | {fmt_money(fs.get('net_investment_income'))} |")
    lines.append(f"| Total investment income | {fmt_money(fs.get('total_investment_income'))} |")
    lines.append(f"| Distributions / share | {fmt_money(fs.get('distributions_per_share'))} |")
    lines.append(f"| Dividends paid | {fmt_money(fs.get('dividends_paid'))} |")
    lines.append(f"| NII / share (derived) | {fmt_money(fs.get('nii_per_share'))} |")
    lines.append(f"| Dividend coverage (NII / divs) | {fmt_num(fs.get('dividend_coverage_nii_over_divs'), 3)} |")
    lines.append(f"| PIK income ratio | {fmt_pct(fs.get('pik_income_ratio'))} |")
    lines.append("")

    # --- NAV trend ---------------------------------------------------------
    lines.append("## NAV trend")
    lines.append("")
    history = nt.get("history") or []
    if history:
        vals = [h["val"] for h in history if h.get("val") is not None]
        spark = sparkline(vals) if vals else ""
        first = history[0]; last = history[-1]
        lines.append(f"- **QoQ:** {fmt_pct(nt.get('qoq_pct_change'))} "
                     f"({arrow_for_delta(nt.get('qoq_pct_change'))})")
        lines.append(f"- **YoY:** {fmt_pct(nt.get('yoy_pct_change'))} "
                     f"({arrow_for_delta(nt.get('yoy_pct_change'))})")
        lines.append(f"- **History ({first.get('end')} → {last.get('end')}):** "
                     f"`{spark}`  "
                     f"range {fmt_money(min(vals))} → {fmt_money(max(vals))}")
        lines.append("")
        lines.append("| Period end | NAV / share | Form |")
        lines.append("|---|---|---|")
        for h in history:
            lines.append(f"| {h.get('end') or 'n/a'} | "
                         f"{fmt_money(h.get('val'))} | "
                         f"{h.get('form') or ''} |")
    else:
        lines.append("_No NAV history available._")
    lines.append("")

    # --- Portfolio snapshot -----------------------------------------------
    lines.append("## Portfolio snapshot")
    lines.append("")
    if not p:
        lines.append("_No Schedule-of-Investments data extracted for this BDC._")
    else:
        lines.append(f"_Schedule of Investments as of {fmt_date(p.get('as_of'))} "
                     f"(from {p.get('source_form') or 'n/a'}, accession "
                     f"{p.get('source_accession') or 'n/a'})._")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Total portfolio FV | {fmt_money(p.get('total_fair_value'))} |")
        lines.append(f"| Industries identified | {p.get('n_industries') if p.get('n_industries') is not None else 'n/a'} |")
        hhi = p.get("industry_hhi")
        eff_n = p.get("industry_hhi_effective_n")
        if hhi is not None and eff_n is None:
            try:
                eff_n = round(1.0 / float(hhi), 2)
            except Exception:
                eff_n = None
        lines.append(f"| Industry HHI | {fmt_num(hhi, 4)} "
                     f"(effective-N ≈ {fmt_num(eff_n, 1)}) |")
        lines.append(f"| Industry coverage | "
                     f"{fmt_pct(p.get('industry_coverage_pct'), 1, already_pct=True)} "
                     f"(method: `{p.get('industry_method') or 'n/a'}`) |")
        lines.append(f"| Largest industry share | "
                     f"{fmt_pct(p.get('largest_industry_pct'), 2, already_pct=True)} |")
        nacc = p.get("non_accrual_pct_fair_value")
        nacc_src = p.get("non_accrual_source")
        lines.append(f"| Non-accrual % (fair value) | "
                     f"{fmt_pct(nacc) if nacc is not None else 'not tagged'}"
                     f"{f' (source: {nacc_src})' if nacc_src else ''} |")
        lines.append("")

        top_ind = p.get("top_industries") or []
        if top_ind:
            lines.append("### Top industries")
            lines.append("")
            lines.append("| # | Industry | Portfolio share |")
            lines.append("|---|---|---|")
            for i, ind in enumerate(top_ind, 1):
                lines.append(f"| {i} | {ind.get('name') or 'n/a'} | "
                             f"{fmt_pct(ind.get('pct_portfolio'), 2, already_pct=True)} |")
            lines.append("")

        mix = p.get("affiliation_mix") or {}
        if mix:
            def _label(s: str) -> str:
                s = s.replace("Investment", "").replace("Issuer", "").strip()
                parts = s.split()
                if not parts:
                    return "Other"
                head = parts[0]
                tail = "".join(parts[1:])
                return f"{head}-{tail}" if tail else head
            lines.append("### Affiliation mix")
            lines.append("")
            lines.append("| Category | Share |")
            lines.append("|---|---|")
            for k, v in sorted(mix.items(), key=lambda kv: -kv[1]):
                lines.append(f"| {_label(k)} | {fmt_pct(v, 2)} |")
            lines.append("")

    # --- Factor audit ------------------------------------------------------
    lines.append("## Factor audit (risk engine v1)")
    lines.append("")
    factors = r.get("factors") or []
    if factors:
        lines.append("| Factor | Raw | Sub-score | Weight | Contribution | Source |")
        lines.append("|---|---|---|---|---|---|")
        for f in factors:
            if f.get("excluded"):
                lines.append(f"| {f.get('name')} | _missing_ | — | "
                             f"{fmt_num(f.get('weight'), 1)} | — | "
                             f"_{f.get('reason') or 'excluded'}_ |")
                continue
            fb = " *(fallback)*" if f.get("used_fallback") else ""
            lines.append(f"| {f.get('name')} | "
                         f"{fmt_num(f.get('raw_value'), 4)} | "
                         f"{fmt_num(f.get('score'), 1)} | "
                         f"{fmt_num(f.get('weight'), 1)} | "
                         f"{fmt_num(f.get('contribution'), 1)} | "
                         f"`{f.get('used_source')}`{fb} |")
    else:
        lines.append("_No factor audit available._")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_{brief.get('disclaimer')}_")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Index roll-up.
# ---------------------------------------------------------------------------

def render_index_markdown(briefs: list[dict], run_date: str | None) -> str:
    lines: list[str] = []
    lines.append(f"# PriCredit investor briefs — {run_date or 'latest'}")
    lines.append("")
    lines.append(f"_Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}. "
                 f"{len(briefs)} BDCs covered._")
    lines.append("")

    # Band roll-up
    from collections import Counter
    bands = Counter((b["risk"]["band"] or "unknown").lower() for b in briefs)
    lines.append("## Band distribution")
    lines.append("")
    lines.append("| Band | Count |")
    lines.append("|---|---|")
    for band in ("critical", "high", "medium", "low", "unknown"):
        lines.append(f"| {band.upper()} | {bands.get(band, 0)} |")
    lines.append("")

    # Sorted table
    ordered = sorted(
        briefs,
        key=lambda b: (
            -(b["risk"].get("composite_score") or -1),
            b.get("ticker") or "",
        ),
    )
    lines.append("## BDCs by composite score (highest risk first)")
    lines.append("")
    lines.append("| Ticker | Score | Band | Alerts | NAV YoY | HHI | Non-accrual | Brief |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in ordered:
        ticker = b.get("ticker") or "?"
        score = b["risk"].get("composite_score")
        band = (b["risk"].get("band") or "unknown").upper()
        n_alerts = len(b.get("alerts") or [])
        yoy = (b.get("nav_trend") or {}).get("yoy_pct_change")
        p = b.get("portfolio") or {}
        hhi = p.get("industry_hhi")
        nacc = p.get("non_accrual_pct_fair_value")
        lines.append(
            f"| {ticker} | {fmt_num(score, 1)} | {band} | {n_alerts} | "
            f"{fmt_pct(yoy) if yoy is not None else 'n/a'} | "
            f"{fmt_num(hhi, 3) if hhi is not None else 'n/a'} | "
            f"{fmt_pct(nacc) if nacc is not None else 'n/a'} | "
            f"[{ticker}.md]({ticker}.md) |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Heuristic scoring (v1). Derived from publicly filed SEC EDGAR data. "
                 "Informational only; not investment advice._")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def _resolve_run_date(reports_root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    # Pick the newest reports/<DATE>/ directory that has at least one risk_*.json
    candidates = sorted(
        [d for d in reports_root.iterdir()
         if d.is_dir() and any(d.glob("risk_*.json"))],
        key=lambda d: d.name,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("[brief] no reports/<DATE>/ with risk_*.json found; "
                         "pass --date explicitly")
    return candidates[0].name


def _summary_for_cik(extracted_root: Path, cik: str) -> dict | None:
    fp = extracted_root / cik / "facts" / "summary.json"
    return _load_json(fp)


def _timeseries_for_cik(extracted_root: Path, cik: str) -> dict | None:
    fp = extracted_root / cik / "facts" / "timeseries.json"
    return _load_json(fp)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--extracted", default=str(DEFAULT_EXTRACTED))
    ap.add_argument("--reports", default=str(DEFAULT_REPORTS))
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    ap.add_argument("--date", default="",
                    help="Run date folder under reports/. Default: latest "
                         "dated folder containing risk_*.json.")
    ap.add_argument("--briefs-dir", default="",
                    help="Override output folder (default: reports/<DATE>/briefs).")
    ap.add_argument("--ticker", default="")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated tickers to restrict to.")
    ap.add_argument("--ciks", default="",
                    help="Comma-separated CIKs to restrict to.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing briefs.")
    ap.add_argument("--print", dest="print_briefs", action="store_true",
                    help="Print each brief to stdout.")
    ap.add_argument("--run-summary", default="",
                    help="Write a JSON summary of the run to this path.")
    args = ap.parse_args()

    extracted_root = Path(args.extracted)
    reports_root = Path(args.reports)
    run_date = _resolve_run_date(reports_root, args.date or None)
    run_dir = reports_root / run_date
    briefs_dir = Path(args.briefs_dir) if args.briefs_dir else run_dir / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)

    wanted_tickers: set[str] = set()
    for t in [args.ticker] + args.tickers.split(","):
        t = (t or "").strip().upper()
        if t:
            wanted_tickers.add(t)
    wanted_ciks = {pad_cik(c.strip()) for c in args.ciks.split(",") if c.strip()}

    risk_paths = sorted(run_dir.glob("risk_*.json"))
    risk_paths = [p for p in risk_paths if p.name != "risk_summary.json"]

    briefs: list[dict] = []
    written: list[Path] = []
    skipped = 0
    errors = 0
    filtering = bool(wanted_tickers or wanted_ciks)

    for rpath in risk_paths:
        risk = _load_json(rpath)
        if not risk:
            errors += 1
            continue
        ticker = (risk.get("ticker") or "").upper()
        cik = risk.get("cik") or ""

        in_scope = True
        if wanted_tickers and ticker not in wanted_tickers:
            in_scope = False
        if wanted_ciks and cik not in wanted_ciks:
            in_scope = False

        md_out = briefs_dir / f"{ticker}.md"
        json_out = briefs_dir / f"{ticker}.json"

        # Out-of-scope BDCs: when a subset filter is active, still pick up
        # any previously-rendered brief so the index keeps covering the
        # full universe instead of shrinking to the filter set.
        if not in_scope:
            if filtering and json_out.exists():
                b = _load_json(json_out)
                if b:
                    briefs.append(b)
            continue

        if md_out.exists() and json_out.exists() and not args.force:
            b = _load_json(json_out)
            if b:
                briefs.append(b)
                skipped += 1
                continue

        summary = _summary_for_cik(extracted_root, cik)
        if not summary:
            print(f"[brief] skip {ticker}: no summary.json for cik={cik}",
                  file=sys.stderr)
            errors += 1
            continue

        portfolio = _latest_portfolio(cik, extracted_root)
        timeseries = _timeseries_for_cik(extracted_root, cik)
        alerts = _alerts_for_ticker(run_dir, ticker)

        brief = build_brief(summary, risk, alerts, portfolio, timeseries)
        md = render_markdown(brief)

        md_out.write_text(md, encoding="utf-8")
        json_out.write_text(json.dumps(brief, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        written.append(md_out)
        briefs.append(brief)

        if args.print_briefs:
            print(md)

        print(f"[brief] {ticker}: score={brief['risk'].get('composite_score')} "
              f"band={brief['risk'].get('band')} alerts={len(brief.get('alerts') or [])} "
              f"-> {md_out.relative_to(reports_root.parent) if reports_root.parent in md_out.parents else md_out}",
              file=sys.stderr)

    # Index
    index_md = render_index_markdown(briefs, run_date)
    (briefs_dir / "index.md").write_text(index_md, encoding="utf-8")
    index_payload = {
        "schema_version": "pricredit.investor_brief_index/v0",
        "run_date": run_date,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_briefs": len(briefs),
        "briefs": [
            {
                "ticker": b.get("ticker"),
                "cik": b.get("cik"),
                "composite_score": b["risk"].get("composite_score"),
                "band": b["risk"].get("band"),
                "n_alerts": len(b.get("alerts") or []),
                "as_of_filing_end": b.get("as_of_filing_end"),
                "has_portfolio": bool(b.get("portfolio")),
                "industry_hhi": (b.get("portfolio") or {}).get("industry_hhi"),
                "non_accrual_pct_fair_value":
                    (b.get("portfolio") or {}).get("non_accrual_pct_fair_value"),
                "nav_yoy_pct_change": (b.get("nav_trend") or {}).get("yoy_pct_change"),
            }
            for b in briefs
        ],
    }
    (briefs_dir / "index.json").write_text(
        json.dumps(index_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"[brief] wrote {len(written)} new briefs, reused {skipped}, "
          f"errors={errors}; index -> {briefs_dir/'index.md'}",
          file=sys.stderr)

    if args.run_summary:
        Path(args.run_summary).write_text(
            json.dumps({
                "schema_version": "pricredit.investor_brief_run/v0",
                "run_date": run_date,
                "briefs_dir": str(briefs_dir),
                "written": len(written),
                "reused": skipped,
                "errors": errors,
                "n_briefs": len(briefs),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
