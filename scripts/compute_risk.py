#!/usr/bin/env python3
"""
PriCredit Risk Engine — v1.

For each BDC with a parsed `extracted/<cik>/facts/summary.json`:
  1. Pull raw values per factor from the summary (latest / derived /
     nav_trend), applying fallbacks when the primary input is missing.
  2. Convert each raw value to a 0-100 sub-score via the piecewise
     linear curve defined in ingest/risk_weights.yaml.
  3. Composite = weight-renormalized average across factors that had
     data. Missing factors are excluded, not penalized.
  4. Map composite to a band (low | medium | high | critical).
  5. Evaluate the (independent) alert rules; emit one JSON per alert
     into reports/<DATE>/alert_*.json so the dispatcher consumes them.

Outputs:
    reports/<DATE>/risk_<ticker>.json       per-BDC scorecard w/ factor audit
    reports/<DATE>/risk_summary.json        roll-up sorted by band + score
    reports/<DATE>/alert_*.json             one file per firing alert rule

Examples:
    scripts/compute_risk.py
    scripts/compute_risk.py --tickers ARCC,MAIN,OBDC --print
    scripts/compute_risk.py --date 2026-04-18 --force
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml required: pip install -r scripts/requirements.txt") from exc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edgar_common import pad_cik  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS = ROOT / "ingest" / "risk_weights.yaml"
DEFAULT_EXTRACTED = ROOT / "extracted"
DEFAULT_REPORTS = ROOT / "reports"
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"


# ---------------------------------------------------------------------------
# Curve + data accessors.
# ---------------------------------------------------------------------------

def get_by_path(summary: dict, path: str) -> float | None:
    """Resolve a dotted path into summary.json. Supported roots:
       latest.<metric>          -> summary.latest[metric].val
       derived.<name>           -> summary.derived[name].val
       nav_trend.<qoq|yoy>.pct_change -> summary.nav_trend[...].pct_change
       portfolio.<field>        -> summary.portfolio[field]  (from extract_portfolio.py)
    """
    parts = path.split(".")
    if not parts:
        return None
    head = parts[0]
    if head == "latest":
        obs = (summary.get("latest") or {}).get(parts[1]) if len(parts) >= 2 else None
        if not obs:
            return None
        val = obs.get("val")
        return _to_float(val)
    if head == "derived":
        if len(parts) < 2:
            return None
        for d in summary.get("derived") or []:
            if d.get("name") == parts[1]:
                return _to_float(d.get("val"))
        return None
    if head == "nav_trend":
        node: Any = summary.get("nav_trend") or {}
        for p in parts[1:]:
            if not isinstance(node, dict):
                return None
            node = node.get(p)
        return _to_float(node)
    if head == "portfolio":
        node: Any = summary.get("portfolio") or {}
        for p in parts[1:]:
            if not isinstance(node, dict):
                return None
            node = node.get(p)
        return _to_float(node)
    return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def interpolate(value: float, curve: list[list[float]],
                direction: str) -> float:
    """Piecewise-linear lookup. Curve is a list of [threshold, score]
    in the direction the operator wrote them:
      - higher_is_better: thresholds descending (best first)
      - lower_is_better:  thresholds ascending (best first)
    Values beyond the endpoints clamp to that end's score."""
    if not curve:
        return 0.0
    # Walk the curve in order; find the segment value falls into.
    for i in range(len(curve) - 1):
        t1, s1 = curve[i]
        t2, s2 = curve[i + 1]
        if direction == "higher_is_better":
            # t1 >= t2; value above t1 -> clamp to s1.
            if value >= t1:
                return float(s1)
            if t2 <= value < t1:
                if t1 == t2:
                    return float((s1 + s2) / 2)
                frac = (t1 - value) / (t1 - t2)
                return float(s1 + frac * (s2 - s1))
        else:  # lower_is_better: t1 <= t2
            if value <= t1:
                return float(s1)
            if t1 < value <= t2:
                if t1 == t2:
                    return float((s1 + s2) / 2)
                frac = (value - t1) / (t2 - t1)
                return float(s1 + frac * (s2 - s1))
    # Past the last segment -> clamp to last score.
    return float(curve[-1][1])


# ---------------------------------------------------------------------------
# Scoring.
# ---------------------------------------------------------------------------

def score_factor(name: str, cfg: dict, summary: dict) -> dict:
    """Evaluate a single factor against a BDC's summary. Returns a dict
    with raw_value/score/weight_share/contribution/reason even when
    the factor is excluded (for auditability)."""
    primary_source = cfg.get("source")
    direction = cfg.get("direction", "higher_is_better")
    curve = cfg.get("curve") or []
    weight = float(cfg.get("weight", 1.0))
    raw = get_by_path(summary, primary_source) if primary_source else None
    used_source = primary_source
    used_curve = curve
    used_direction = direction
    used_fallback = False

    if raw is None and cfg.get("fallback"):
        fb = cfg["fallback"]
        raw = get_by_path(summary, fb.get("source", ""))
        used_source = fb.get("source")
        used_curve = fb.get("curve") or []
        used_direction = fb.get("direction", "higher_is_better")
        used_fallback = True

    if raw is None:
        return {
            "name": name,
            "weight": weight,
            "raw_value": None,
            "used_source": used_source,
            "used_fallback": used_fallback,
            "score": None,
            "excluded": True,
            "reason": "input missing",
        }

    score = interpolate(raw, used_curve, used_direction)
    return {
        "name": name,
        "weight": weight,
        "raw_value": raw,
        "used_source": used_source,
        "used_fallback": used_fallback,
        "direction": used_direction,
        "curve": used_curve,
        "score": round(score, 2),
        "excluded": False,
    }


def compute_composite(factors: list[dict], bands_cfg: dict) -> tuple[float, str]:
    included = [f for f in factors if not f.get("excluded")]
    if not included:
        return 0.0, "unknown"
    total_w = sum(f["weight"] for f in included)
    if total_w <= 0:
        return 0.0, "unknown"
    composite = sum(f["score"] * f["weight"] for f in included) / total_w
    # Sort bands by max ascending, pick the first whose max the composite
    # falls under. Tolerant of any ordering in the YAML.
    ordered = sorted(bands_cfg.items(), key=lambda kv: kv[1]["max"])
    for _, info in ordered:
        if composite < info["max"]:
            return round(composite, 2), info.get("label", "unknown")
    return round(composite, 2), ordered[-1][1].get("label", "critical")


# ---------------------------------------------------------------------------
# Alerts.
# ---------------------------------------------------------------------------

_OPS = {
    "<":  lambda a, b: a <  b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a >  b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def evaluate_alert(rule_name: str, rule_cfg: dict, summary: dict) -> dict | None:
    triggered: list[dict] = []
    for cond in rule_cfg.get("any_of") or []:
        src = cond.get("source")
        op = cond.get("op")
        thr = cond.get("value")
        requires_missing = cond.get("requires_missing")
        if requires_missing and get_by_path(summary, requires_missing) is not None:
            continue
        raw = get_by_path(summary, src) if src else None
        if raw is None:
            continue
        fn = _OPS.get(op)
        if not fn:
            continue
        try:
            hit = fn(raw, thr)
        except Exception:
            hit = False
        if hit:
            triggered.append({
                "source": src,
                "value": raw,
                "op": op,
                "threshold": thr,
            })
    if not triggered:
        return None
    return {
        "reason": rule_name,
        "severity": rule_cfg.get("severity", "medium"),
        "group": rule_cfg.get("group"),
        "description": rule_cfg.get("description", ""),
        "triggers": triggered,
    }


_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def dedupe_by_group(alerts: list[dict]) -> list[dict]:
    """Within each group, keep only the highest-severity alert. Alerts
    without a group are kept as-is. Stable order otherwise."""
    best_in_group: dict[str, dict] = {}
    ungrouped: list[dict] = []
    order: list[str] = []
    for a in alerts:
        g = a.get("group")
        if not g:
            ungrouped.append(a)
            continue
        if g not in best_in_group:
            best_in_group[g] = a
            order.append(g)
            continue
        cur = best_in_group[g]
        if _SEVERITY_RANK.get(a.get("severity"), 0) > _SEVERITY_RANK.get(cur.get("severity"), 0):
            best_in_group[g] = a
    return [best_in_group[g] for g in order] + ungrouped


# ---------------------------------------------------------------------------
# Scoring driver.
# ---------------------------------------------------------------------------

def score_bdc(summary: dict, weights_cfg: dict) -> dict:
    factors: list[dict] = []
    for name, fcfg in (weights_cfg.get("factors") or {}).items():
        factors.append(score_factor(name, fcfg, summary))

    included = [f for f in factors if not f.get("excluded")]
    total_w_included = sum(f["weight"] for f in included) or 1.0
    for f in factors:
        if f.get("excluded"):
            f["weight_share"] = 0.0
            f["contribution"] = 0.0
        else:
            f["weight_share"] = round(f["weight"] / total_w_included, 4)
            f["contribution"] = round(f["score"] * f["weight"] / total_w_included, 2)

    composite, band = compute_composite(factors, weights_cfg.get("bands") or {})

    raw_alerts: list[dict] = []
    for rule_name, rule_cfg in (weights_cfg.get("alerts") or {}).items():
        a = evaluate_alert(rule_name, rule_cfg, summary)
        if a:
            raw_alerts.append(a)
    alerts = dedupe_by_group(raw_alerts)

    return {
        "composite_score": composite,
        "band": band,
        "factors": factors,
        "n_factors_used": len(included),
        "n_factors_missing": len(factors) - len(included),
        "alerts": alerts,
    }


def build_scorecard(summary: dict, scored: dict, date_str: str,
                    weights_version: str) -> dict:
    return {
        "schema_version": "pricredit.compute_risk/v1",
        "weights_version": weights_version,
        "run_date": date_str,
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cik": summary.get("cik"),
        "ticker": summary.get("ticker"),
        "company_name": summary.get("company_name"),
        "as_of_filing_end": _latest_end(summary),
        "composite_score": scored["composite_score"],
        "band": scored["band"],
        "n_factors_used": scored["n_factors_used"],
        "n_factors_missing": scored["n_factors_missing"],
        "factors": scored["factors"],
        "alerts": scored["alerts"],
        "disclaimer": (
            "Heuristic risk score derived from publicly filed XBRL facts. "
            "Informational only; not investment advice. "
            "Weights and curves are hand-picked (v1) and subject to revision."
        ),
    }


def _latest_end(summary: dict) -> str | None:
    nav = (summary.get("latest") or {}).get("nav_per_share")
    if nav:
        return nav.get("end")
    for obs in (summary.get("latest") or {}).values():
        if isinstance(obs, dict) and obs.get("end"):
            return obs["end"]
    return None


# ---------------------------------------------------------------------------
# Alert file writer (matches idvault alert_*.json shape so the same email
# dispatcher can ingest it later).
# ---------------------------------------------------------------------------

def write_alert_files(scorecard: dict, out_dir: Path, compact_date: str,
                      counter_start: int) -> tuple[list[Path], int]:
    written: list[Path] = []
    counter = counter_start
    ticker = scorecard.get("ticker") or "UNKN"
    for a in scorecard.get("alerts") or []:
        counter += 1
        alert_id = f"RISK-{ticker}-{compact_date}-{counter:03d}"
        case_id = f"CASE-{compact_date}-{ticker}-{a['reason']}"
        doc = {
            "schema_version": "pricredit.alert/v1",
            "alert_id": alert_id,
            "case_id": case_id,
            "platform": "edgar",
            "alert_reason": a["reason"],
            "severity": a["severity"],
            "description": a["description"],
            "scanned_at": scorecard.get("as_of_utc"),
            "bdc": {
                "cik": scorecard.get("cik"),
                "ticker": ticker,
                "company_name": scorecard.get("company_name"),
                "as_of_filing_end": scorecard.get("as_of_filing_end"),
            },
            "composite_score": scorecard.get("composite_score"),
            "band": scorecard.get("band"),
            "triggers": a.get("triggers") or [],
            "llm_summary": None,
            "llm_summary_sources": [],
            "disclaimer": scorecard.get("disclaimer"),
        }
        fp = out_dir / f"alert_{alert_id}.json"
        fp.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                      encoding="utf-8")
        written.append(fp)
    return written, counter


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _load_universe(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {b["cik"]: b for b in (doc.get("bdcs") or []) if b.get("cik")}


def _iter_summaries(extracted_root: Path) -> list[Path]:
    return sorted(extracted_root.glob("*/facts/summary.json"))


def _print_scorecard(sc: dict) -> None:
    print(f"\n=== {sc['ticker']}  score={sc['composite_score']}  "
          f"band={sc['band'].upper()}  "
          f"({sc['n_factors_used']}/{sc['n_factors_used']+sc['n_factors_missing']} factors)",
          file=sys.stderr)
    for f in sc["factors"]:
        if f.get("excluded"):
            print(f"  {f['name']:22s} (excluded: {f.get('reason')})", file=sys.stderr)
            continue
        rv = f["raw_value"]
        rv_s = f"{rv:.4f}" if isinstance(rv, (int, float)) else str(rv)
        print(f"  {f['name']:22s} raw={rv_s:>10}  score={f['score']:>5}"
              f"  w={f['weight']:.1f}  contrib={f['contribution']:>5.1f}"
              f"{'  [fallback]' if f.get('used_fallback') else ''}",
              file=sys.stderr)
    for a in sc["alerts"]:
        print(f"  ALERT [{a['severity'].upper()}] {a['reason']}: {a['description']}",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    ap.add_argument("--extracted", default=str(DEFAULT_EXTRACTED))
    ap.add_argument("--reports", default=str(DEFAULT_REPORTS))
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d", time.gmtime()))
    ap.add_argument("--ticker", default="", help="Single ticker shortcut.")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated tickers to restrict to.")
    ap.add_argument("--ciks", default="",
                    help="Comma-separated CIKs to restrict to.")
    ap.add_argument("--force", action="store_true",
                    help="Re-score even if risk_<ticker>.json already exists.")
    ap.add_argument("--print", dest="print_cards", action="store_true",
                    help="Print each scorecard to stderr.")
    args = ap.parse_args()

    weights_cfg = yaml.safe_load(Path(args.weights).read_text(encoding="utf-8"))
    weights_version = (weights_cfg or {}).get("version", "unknown")

    out_dir = Path(args.reports) / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    compact_date = args.date.replace("-", "")

    tickers = []
    if args.ticker:
        tickers.append(args.ticker.strip().upper())
    if args.tickers:
        tickers.extend(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    wanted_tickers = set(tickers)
    wanted_ciks = {pad_cik(c.strip()) for c in args.ciks.split(",") if c.strip()}

    cards: list[dict] = []
    alerts_written_total = 0
    counter = 0

    for summary_path in _iter_summaries(Path(args.extracted)):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[risk] skip {summary_path}: {exc}", file=sys.stderr)
            continue

        cik = summary.get("cik")
        ticker = (summary.get("ticker") or "").upper()
        if wanted_tickers and ticker not in wanted_tickers:
            continue
        if wanted_ciks and cik not in wanted_ciks:
            continue

        scored = score_bdc(summary, weights_cfg)
        scorecard = build_scorecard(summary, scored, args.date, weights_version)

        card_path = out_dir / f"risk_{ticker or cik}.json"
        if card_path.exists() and not args.force:
            # freshness guard: keep today's output stable unless --force.
            pass
        card_path.write_text(json.dumps(scorecard, indent=2, ensure_ascii=False),
                             encoding="utf-8")

        alert_files, counter = write_alert_files(scorecard, out_dir,
                                                 compact_date, counter)
        alerts_written_total += len(alert_files)
        cards.append(scorecard)

        if args.print_cards:
            _print_scorecard(scorecard)

    if not cards:
        print("[risk] no BDC summaries matched; nothing to score.", file=sys.stderr)
        return 0

    # Roll-up: sort by composite desc so the riskiest appear first.
    cards.sort(key=lambda c: (c["composite_score"] or 0.0), reverse=True)
    rollup = {
        "schema_version": "pricredit.risk_summary/v1",
        "weights_version": weights_version,
        "run_date": args.date,
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_bdcs_scored": len(cards),
        "band_counts": _tally(c["band"] for c in cards),
        "alerts_emitted": alerts_written_total,
        "bdcs": [{
            "ticker": c.get("ticker"),
            "cik": c.get("cik"),
            "composite_score": c.get("composite_score"),
            "band": c.get("band"),
            "as_of_filing_end": c.get("as_of_filing_end"),
            "alert_reasons": [a["reason"] for a in c.get("alerts") or []],
            "factor_summary": {f["name"]: f.get("score") for f in c.get("factors") or []},
        } for c in cards],
        "disclaimer": cards[0].get("disclaimer"),
    }
    (out_dir / "risk_summary.json").write_text(
        json.dumps(rollup, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[risk] scored={len(cards)} alerts={alerts_written_total} "
          f"bands={rollup['band_counts']} -> {out_dir}", file=sys.stderr)
    return 0


def _tally(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
