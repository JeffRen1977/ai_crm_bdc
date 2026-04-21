#!/usr/bin/env python3
"""
ARCC-first Shadow NAV beta estimator.

This script intentionally produces a conservative, explainable
"event-adjusted NAV signal" from:
  - extracted/<cik>/facts/summary.json (official latest NAV baseline)
  - extracted/<cik>/events8k/*/events_8k.json (minimal 8-K event signals)

It does NOT replace official NAV. Outputs are informational only.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXTRACTED = ROOT / "extracted"
DEFAULT_REPORTS = ROOT / "reports"
DEFAULT_CIK = "0001287750"
DEFAULT_TICKER = "ARCC"

# Conservative bps assumptions for v0 beta.
ADJ_BPS = {
    "credit_facility_change": 10,   # +0.10% NAV heuristic
    "realized_gain_loss": 25,       # +/-0.25% from directional inference
}


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_nav(summary: dict) -> tuple[float | None, dict]:
    nav = ((summary.get("latest") or {}).get("nav_per_share") or {})
    try:
        return float(nav.get("val")), nav
    except Exception:
        return None, nav


def _infer_gain_loss_direction(event_doc: dict) -> int:
    """
    Returns:
      +1 if text suggests realized gain,
      -1 if text suggests realized loss,
       0 unknown/mixed.
    """
    hits = (((event_doc.get("signals") or {}).get("keyword_hits") or {})
            .get("realized_gain_loss") or [])
    blob = " ".join((h.get("excerpt") or "").lower() for h in hits)
    has_gain = ("realized gain" in blob) or ("realized gains" in blob)
    has_loss = ("realized loss" in blob) or ("realized losses" in blob)
    if has_gain and not has_loss:
        return 1
    if has_loss and not has_gain:
        return -1
    return 0


def _event_adjustments(events: list[dict], baseline_filed: str | None) -> tuple[list[dict], float]:
    """
    Return list of additive adjustments in decimal NAV-percent terms
    (e.g., +0.001 == +10 bps) and summed adjustment.
    """
    out: list[dict] = []
    total = 0.0
    for ev in events:
        filing_date = ev.get("filing_date") or ""
        # only events after official baseline filing date
        if baseline_filed and filing_date and filing_date <= baseline_filed:
            continue

        signals = (ev.get("signals") or {}).get("flags") or {}

        if signals.get("credit_facility_change"):
            adj = ADJ_BPS["credit_facility_change"] / 10000.0
            out.append({
                "accession_number": ev.get("accession_number"),
                "filing_date": filing_date,
                "kind": "credit_facility_change",
                "nav_pct_adjustment": adj,
                "rationale": "Heuristic +10bps for financing flexibility event.",
            })
            total += adj

        if signals.get("realized_gain_loss"):
            direction = _infer_gain_loss_direction(ev)
            if direction == 0:
                continue
            sign = 1.0 if direction > 0 else -1.0
            adj = sign * (ADJ_BPS["realized_gain_loss"] / 10000.0)
            out.append({
                "accession_number": ev.get("accession_number"),
                "filing_date": filing_date,
                "kind": "realized_gain_loss",
                "direction": "gain" if direction > 0 else "loss",
                "nav_pct_adjustment": adj,
                "rationale": "Heuristic +/-25bps from realized gain/loss language.",
            })
            total += adj
    return out, total


def _confidence_label(adjustments: list[dict]) -> str:
    # Keep confidence conservative until event parsing matures.
    n = len(adjustments)
    if n >= 3:
        return "medium"
    if n >= 1:
        return "low"
    return "low"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extracted", default=str(DEFAULT_EXTRACTED),
                    help="Root for extracted artifacts (default: extracted/).")
    ap.add_argument("--reports", default=str(DEFAULT_REPORTS),
                    help="Root for reports (default: reports/).")
    ap.add_argument("--cik", default=DEFAULT_CIK,
                    help=f"Target CIK (default: {DEFAULT_CIK}).")
    ap.add_argument("--ticker", default=DEFAULT_TICKER,
                    help=f"Ticker label for output file (default: {DEFAULT_TICKER}).")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d", time.gmtime()),
                    help="Output report date directory (default: today UTC).")
    ap.add_argument("--print", action="store_true",
                    help="Print result JSON to stdout.")
    args = ap.parse_args()

    cik = str(args.cik).strip().zfill(10)
    extracted_root = Path(args.extracted)
    reports_root = Path(args.reports)

    summary_path = extracted_root / cik / "facts" / "summary.json"
    summary = _load_json(summary_path)
    if not summary:
        raise SystemExit(f"summary not found or unreadable: {summary_path}")

    baseline_nav, nav_obs = _latest_nav(summary)
    if baseline_nav is None:
        raise SystemExit("summary.latest.nav_per_share.val missing; cannot compute shadow NAV")

    events = []
    events_root = extracted_root / cik / "events8k"
    for fp in sorted(events_root.glob("*/events_8k.json")):
        doc = _load_json(fp)
        if doc:
            events.append(doc)

    baseline_filed = nav_obs.get("filed")
    adjustments, total_pct_adj = _event_adjustments(events, baseline_filed)
    shadow_nav = baseline_nav * (1.0 + total_pct_adj)
    confidence = _confidence_label(adjustments)

    out = {
        "schema_version": "pricredit.shadow_nav/v0",
        "cik": cik,
        "ticker": args.ticker,
        "company_name": summary.get("company_name"),
        "baseline": {
            "nav_per_share": baseline_nav,
            "from_filing_end": nav_obs.get("end"),
            "from_filing_date": baseline_filed,
            "from_accession": nav_obs.get("accn"),
        },
        "shadow_nav_beta": {
            "nav_per_share_estimate": round(shadow_nav, 4),
            "total_nav_pct_adjustment": round(total_pct_adj, 6),
            "total_nav_bps_adjustment": round(total_pct_adj * 10000, 2),
            "confidence": confidence,
            "status": "experimental",
        },
        "adjustments": adjustments,
        "coverage": {
            "events_scanned": len(events),
            "events_used": len({a["accession_number"] for a in adjustments}),
            "adjustment_rows": len(adjustments),
        },
        "assumptions": {
            "credit_facility_change_bps": ADJ_BPS["credit_facility_change"],
            "realized_gain_loss_bps": ADJ_BPS["realized_gain_loss"],
            "baseline_filter": "Use only 8-K filings after latest official NAV filing date.",
        },
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": (
            "Experimental event-adjusted NAV signal derived from heuristic 8-K extraction. "
            "Informational only; not investment advice; verify against original SEC filings."
        ),
    }

    out_dir = reports_root / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shadow_nav_{args.ticker}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"[shadow_nav] wrote {out_path}", flush=True)
    if args.print:
        print(json.dumps(out, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
