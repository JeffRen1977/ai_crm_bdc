#!/usr/bin/env python3
"""
pc — PriCredit query CLI (read-only BDC risk + portfolio lookup).

Designed as the single entry point for *programmatic* consumers
(WhatsApp bridge, Slack bridge, cron digests, local shell) that need
chat-sized answers about BDC risk from the daily PriCredit pipeline
outputs. All output is plain text, ≤ ~4000 chars so it fits in one
WhatsApp message.

Data source: `reports/<DATE>/` (risk_<TICKER>.json, risk_summary.json,
alert_RISK-*.json, briefs/<TICKER>.md). The CLI auto-picks the
newest date folder that contains at least a risk_summary.json.

No side effects — this script never writes, never hits EDGAR, never
triggers the pipeline. Safe to invoke from an untrusted caller as
long as the caller restricts the `ticker` argument to `[A-Z.\\-]+`.

Commands:
    pc help                        list commands
    pc date                        resolve + print which reports/<DATE> is in use
    pc status <TICKER>             one-paragraph risk snapshot
    pc brief  <TICKER>             condensed per-BDC investor brief
    pc alerts [DATE] [--severity]  list open alerts
    pc digest [DATE]               universe-wide digest (same format as
                                   send-whatsapp-alerts.sh --digest)
    pc top    [N]                  top-N riskiest BDCs by composite score

Examples:
    pc status ARCC
    pc alerts --severity critical
    pc digest 2026-04-18
    pc top 10

Exit codes:
    0   success
    1   usage error / unknown command
    2   no data (no reports/<DATE>/ exists, or ticker not scored today)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Project root = parent of this script. Using pathlib so symlinked
# wrappers still resolve to the right repo.
ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR_DEFAULT = ROOT / "reports"

# Mirrors send_whatsapp_alerts.py for a consistent look on the phone.
TIER_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_EMOJI = {
    "critical": "🛑",
    "high":     "🔴",
    "medium":   "🟠",
    "low":      "🟡",
    "info":     "🔵",
    "unknown":  "⚪",
}

# Any caller-supplied ticker is validated against this. Prevents a
# malicious @pc payload from sneaking a path traversal into the file
# lookup (e.g., "../../etc/passwd"). BDC tickers in our universe are
# uppercase letters, occasionally with a dot or dash.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Bank conventions for which metric sources are percents (same as
# send_risk_alerts.py). Kept local to avoid a cross-import.
_PERCENT_SUFFIXES = (
    ".pct_change",
    ".asset_coverage_ratio",
    ".pik_income_ratio",
    ".fair_value_to_cost",
    ".dividend_coverage_nii_over_divs",
    ".debt_to_assets",
)


# ---------------------------------------------------------------------------
# Date + path resolution.
# ---------------------------------------------------------------------------

def _is_date_dir(name: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", name))


def resolve_reports_dir(date: str | None,
                        reports_root: Path = REPORTS_DIR_DEFAULT) -> Path | None:
    """Pick the reports directory to read.

    If `date` is given, return `reports/<date>` (accepts the "final" /
    "pv0" suffixes the pipeline sometimes writes, e.g. "2026-04-18-final").
    Otherwise, pick the newest subdir whose name starts with YYYY-MM-DD
    **and that contains a risk_summary.json**. This lets us ignore
    half-finished run dirs without briefs.
    """
    if not reports_root.exists():
        return None

    if date:
        # Exact match first; then try `<date>-*` suffixed siblings sorted
        # by mtime (newest wins). This covers the "-final" / "-pv0"
        # pattern you used during testing.
        exact = reports_root / date
        if exact.is_dir():
            return exact
        suffixed = sorted(
            (p for p in reports_root.iterdir()
             if p.is_dir() and p.name.startswith(date + "-")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return suffixed[0] if suffixed else None

    candidates = [p for p in reports_root.iterdir()
                  if p.is_dir() and _is_date_dir(p.name)
                  and (p / "risk_summary.json").exists()]
    if not candidates:
        return None
    # Sort by (date_prefix, has_briefs, name_length, mtime) descending.
    # Same-day variants: prefer the one with a briefs/ directory, then
    # the longer name (so "2026-04-18-final" wins over "2026-04-18"),
    # then the most recently modified.
    candidates.sort(
        key=lambda p: (
            p.name[:10],
            1 if (p / "briefs").is_dir() else 0,
            len(p.name),
            p.stat().st_mtime,
        ),
        reverse=True,
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Formatting helpers.
# ---------------------------------------------------------------------------

def _fmt_val(v, source: str | None = None) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and source and any(
                source.endswith(suf) for suf in _PERCENT_SUFFIXES):
            return f"{v * 100:.2f}%"
        return f"{v:.4f}" if isinstance(v, float) else str(v)
    return str(v)


def _severity_rank(s: str | None) -> int:
    return TIER_ORDER.get((s or "low").lower(), 0)


def _edgar_filings_url(cik: str | None) -> str:
    if not cik:
        return ""
    try:
        n = int(str(cik).lstrip("0") or "0")
    except ValueError:
        return ""
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={n}&type=10-K&dateb=&owner=include&count=40"
    )


def _severity_emoji(band: str | None) -> str:
    return SEVERITY_EMOJI.get((band or "low").lower(), "")


def _clip(text: str, limit: int = 3800) -> str:
    """WhatsApp messages above ~4000 chars get fragmented. We keep a
    small buffer for the caller's framing + the "... (truncated)"
    footer."""
    if len(text) <= limit:
        return text
    return text[:limit - 20].rstrip() + "\n… (truncated)"


def _validate_ticker(t: str) -> str:
    t = t.strip().upper()
    if not _TICKER_RE.match(t):
        raise SystemExit(
            f"invalid ticker {t!r} (allowed: {_TICKER_RE.pattern})")
    return t


# ---------------------------------------------------------------------------
# Command: pc date
# ---------------------------------------------------------------------------

def cmd_date(args: argparse.Namespace, reports_root: Path) -> int:
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory with risk_summary.json found",
              file=sys.stderr)
        return 2
    n_risk = len(list(d.glob("risk_*.json"))) - (1 if (d / "risk_summary.json").exists() else 0)
    n_alerts = len(list(d.glob("alert_RISK-*.json")))
    has_briefs = (d / "briefs").is_dir()
    print(f"reports dir: {d.relative_to(ROOT) if d.is_relative_to(ROOT) else d}")
    print(f"risk cards : {max(n_risk, 0)}")
    print(f"alerts     : {n_alerts}")
    print(f"briefs     : {'yes' if has_briefs else 'no'}")
    return 0


# ---------------------------------------------------------------------------
# Command: pc status <TICKER>
# ---------------------------------------------------------------------------

def _format_factor_line(f: dict) -> str:
    """One tight line per factor for the status card."""
    name = f.get("name", "?")
    score = f.get("score")
    contrib = f.get("contribution")
    source = f.get("used_source")
    raw = f.get("raw_value")
    ex = " [excluded]" if f.get("excluded") else ""
    return (f"  {name:<22} raw={_fmt_val(raw, source)} "
            f"score={score:.1f} contrib={contrib:.1f}{ex}"
            if score is not None and contrib is not None
            else f"  {name:<22} (no data){ex}")


def cmd_status(args: argparse.Namespace, reports_root: Path) -> int:
    ticker = _validate_ticker(args.ticker)
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory found", file=sys.stderr)
        return 2

    card_path = d / f"risk_{ticker}.json"
    if not card_path.exists():
        print(f"{ticker} not scored in {d.name}. "
              f"Run: pc top 200 | grep {ticker}   to check spelling.",
              file=sys.stderr)
        return 2

    r = json.loads(card_path.read_text(encoding="utf-8"))
    score = r.get("composite_score")
    band = r.get("band") or "?"
    emoji = _severity_emoji(band)
    company = r.get("company_name") or ticker
    as_of = r.get("as_of_filing_end") or "?"
    cik = r.get("cik") or ""

    lines = [
        f"🏦 {ticker} · {emoji} {band.upper()} · score {score}",
        f"{company.strip()}",
        f"as_of {as_of} · run {r.get('run_date', d.name)}",
        "",
        "Factors (raw → score, weight share × curve):",
    ]
    # Sort by descending contribution so the dominant drivers are at the top.
    factors = sorted(
        (r.get("factors") or []),
        key=lambda f: (f.get("contribution") or 0),
        reverse=True,
    )
    for f in factors:
        lines.append(_format_factor_line(f))

    n_used = r.get("n_factors_used", len([f for f in factors if not f.get("excluded")]))
    n_miss = r.get("n_factors_missing", 0)
    lines.append("")
    lines.append(f"factors used={n_used} missing={n_miss}")

    url = _edgar_filings_url(cik)
    if url:
        lines.append(f"EDGAR: {url}")
    return _print(_clip("\n".join(lines)))


# ---------------------------------------------------------------------------
# Command: pc brief <TICKER>
# ---------------------------------------------------------------------------

def cmd_brief(args: argparse.Namespace, reports_root: Path) -> int:
    ticker = _validate_ticker(args.ticker)
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory found", file=sys.stderr)
        return 2

    brief_md = d / "briefs" / f"{ticker}.md"
    if not brief_md.exists():
        # Degrade to a status-like summary if the brief wasn't generated.
        sys.stderr.write(
            f"no brief on disk for {ticker} in {d.name}; "
            f"falling back to status card\n")
        args.ticker = ticker
        return cmd_status(args, reports_root)

    text = brief_md.read_text(encoding="utf-8")
    # Briefs are markdown; strip obvious chrome (front-matter-style
    # horizontal rules, repeated blank lines) so what lands in WhatsApp
    # is readable as plain text.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    return _print(_clip(text))


# ---------------------------------------------------------------------------
# Command: pc alerts [DATE] [--severity high|critical|...]
# ---------------------------------------------------------------------------

def cmd_alerts(args: argparse.Namespace, reports_root: Path) -> int:
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory found", file=sys.stderr)
        return 2

    paths = sorted(d.glob("alert_RISK-*.json"))
    if not paths:
        return _print(f"No alerts in {d.name}.")

    min_rank = _severity_rank(args.severity) if args.severity else 0
    rows: list[tuple[str, dict]] = []  # (ticker, alert)
    for p in paths:
        try:
            a = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _severity_rank(a.get("severity")) < min_rank:
            continue
        tkr = (a.get("bdc") or {}).get("ticker") or p.name.split("-")[1]
        rows.append((tkr, a))

    if not rows:
        return _print(f"No alerts ≥ {args.severity} in {d.name}.")

    # Group by severity → ticker for compact display
    rows.sort(key=lambda r: (
        -_severity_rank(r[1].get("severity")),
        -(r[1].get("composite_score") or 0),
        r[0],
    ))

    lines = [
        f"🏦 PriCredit alerts · {d.name} · {len(rows)} "
        f"{'total' if not args.severity else '≥ ' + args.severity}",
    ]
    for tkr, a in rows[:40]:
        sev = (a.get("severity") or "low").lower()
        lines.append(
            f"{_severity_emoji(sev)} {tkr:<6} "
            f"{a.get('alert_reason', '?'):<28} "
            f"score {a.get('composite_score')} "
            f"({a.get('band') or '?'})"
        )
    if len(rows) > 40:
        lines.append(f"… +{len(rows) - 40} more")
    return _print(_clip("\n".join(lines)))


# ---------------------------------------------------------------------------
# Command: pc digest [DATE]
# ---------------------------------------------------------------------------

def cmd_digest(args: argparse.Namespace, reports_root: Path) -> int:
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory found", file=sys.stderr)
        return 2

    summary_path = d / "risk_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    n_scored = summary.get("n_bdcs_scored")
    bands = summary.get("band_counts") or {}
    band_str = " ".join(f"{k}={v}" for k, v in sorted(bands.items()))

    # Rank BDCs by composite_score to surface the worst actors.
    bdcs = summary.get("bdcs") or []
    bdcs = sorted(bdcs, key=lambda b: (b.get("composite_score") or 0),
                  reverse=True)

    n_alerts = len(list(d.glob("alert_RISK-*.json")))

    lines = [
        f"🏦 PriCredit digest · {d.name} · {n_alerts} alerts",
    ]
    subhead = []
    if n_scored is not None:
        subhead.append(f"scored {n_scored}")
    if band_str:
        subhead.append(f"bands {band_str}")
    if subhead:
        lines.append(" · ".join(subhead))

    for b in bdcs[:15]:
        reasons = ", ".join((b.get("alert_reasons") or [])[:2]) or "—"
        lines.append(
            f"{_severity_emoji(b.get('band'))} "
            f"{(b.get('ticker') or '?'):<6} "
            f"{reasons} · score {b.get('composite_score')} "
            f"({b.get('band') or '?'})"
        )
    if len(bdcs) > 15:
        lines.append(f"… +{len(bdcs) - 15} more BDCs")
    lines.append(f"Report: {d.name}/briefs/index.md")
    return _print(_clip("\n".join(lines)))


# ---------------------------------------------------------------------------
# Command: pc top [N]
# ---------------------------------------------------------------------------

def cmd_top(args: argparse.Namespace, reports_root: Path) -> int:
    d = resolve_reports_dir(args.date, reports_root)
    if not d:
        print("no reports/<DATE>/ directory found", file=sys.stderr)
        return 2
    summary_path = d / "risk_summary.json"
    if not summary_path.exists():
        print(f"no risk_summary.json in {d.name}", file=sys.stderr)
        return 2
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    n = max(1, min(args.n, 200))
    bdcs = sorted(
        summary.get("bdcs") or [],
        key=lambda b: (b.get("composite_score") or 0),
        reverse=True,
    )[:n]

    lines = [f"🏦 Top {len(bdcs)} by composite score · {d.name}"]
    for i, b in enumerate(bdcs, 1):
        reasons = ", ".join((b.get("alert_reasons") or [])[:2])
        tail = f" · {reasons}" if reasons else ""
        lines.append(
            f"{i:>2}. {_severity_emoji(b.get('band'))} "
            f"{(b.get('ticker') or '?'):<6} "
            f"{b.get('composite_score')} ({b.get('band') or '?'}){tail}"
        )
    return _print(_clip("\n".join(lines)))


# ---------------------------------------------------------------------------
# Command: pc help
# ---------------------------------------------------------------------------

def cmd_help(_args: argparse.Namespace, _reports_root: Path) -> int:
    return _print(
        "PriCredit CLI (read-only):\n"
        "  pc status <TICKER>          one-paragraph risk snapshot\n"
        "  pc brief  <TICKER>          per-BDC investor brief\n"
        "  pc alerts [DATE] [--severity S]  list open alerts\n"
        "  pc digest [DATE]            universe-wide digest\n"
        "  pc top    [N]               top-N riskiest BDCs today\n"
        "  pc date                     which reports/<DATE> is \"today\"\n"
        "  pc help                     this message\n"
        "\n"
        "DATE is optional; omitted → auto-pick the newest dir with data.\n"
    )


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def _print(text: str) -> int:
    """Single output point so it's easy to cap or redirect. Also
    handles BrokenPipeError for `pc ... | head`."""
    try:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pc",
        description="PriCredit query CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--reports-root", default=str(REPORTS_DIR_DEFAULT),
                    help=f"Reports root (default: {REPORTS_DIR_DEFAULT})")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("help", help="list commands")
    p.set_defaults(func=cmd_help, date=None)

    p = sub.add_parser("date", help="which reports/<DATE> is selected")
    p.add_argument("date", nargs="?")
    p.set_defaults(func=cmd_date)

    p = sub.add_parser("status", help="per-BDC risk snapshot")
    p.add_argument("ticker")
    p.add_argument("--date", help="override reports/<DATE>")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("brief", help="per-BDC investor brief")
    p.add_argument("ticker")
    p.add_argument("--date", help="override reports/<DATE>")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("alerts", help="list open alerts")
    p.add_argument("date", nargs="?")
    p.add_argument("--severity",
                   choices=["info", "low", "medium", "high", "critical"])
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("digest", help="universe-wide digest")
    p.add_argument("date", nargs="?")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("top", help="top-N riskiest BDCs today")
    p.add_argument("n", nargs="?", type=int, default=10)
    p.add_argument("--date", help="override reports/<DATE>")
    p.set_defaults(func=cmd_top)

    args = ap.parse_args(argv)
    reports_root = Path(args.reports_root).expanduser()

    if not args.cmd:
        return cmd_help(args, reports_root)
    return args.func(args, reports_root)


if __name__ == "__main__":
    raise SystemExit(main())
