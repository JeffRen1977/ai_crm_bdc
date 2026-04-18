#!/usr/bin/env python3
"""
PriCredit — Risk alert dispatcher (email / SMTP).

Reads alert JSON files produced by scripts/compute_risk.py and emails
each one to the recipients listed in ingest/notifications.yaml. Shares
a schema and an operational shape with idvault's send_warnings.py so
the two pipelines can borrow from each other.

Credentials stay in environment variables (expected in ~/.pricredit-env):

    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USER       SMTP login username
    SMTP_PASSWORD   password / Gmail App Password
    SMTP_FROM       (optional) "PriCredit <id@example>"; else SMTP_USER
    SMTP_STARTTLS   "1" (default) | "0"
    SMTP_USE_SSL    "1" to use SMTPS on port 465; default "0"

Idempotency: for each alert we write reports/<DATE>/.sent/<alert_id>.json
once the SMTP server accepts the message, so reruns skip it.

Usage:
    scripts/send_risk_alerts.py --reports-dir reports/2026-04-18
    scripts/send_risk_alerts.py --alert reports/2026-04-18/alert_RISK-...json
    scripts/send_risk_alerts.py --reports-dir reports/2026-04-18 --dry-run
    scripts/send_risk_alerts.py --reports-dir reports/2026-04-18 --digest
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml required: pip install -r scripts/requirements.txt") from exc


# Severity ranking must match the `bands` order in ingest/risk_weights.yaml.
# Higher rank = louder alert; threshold comparison is >=.
TIER_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


# ---------------------------------------------------------------------------
# Filtering.
# ---------------------------------------------------------------------------

def _severity_rank(sev: str | None) -> int:
    return TIER_ORDER.get((sev or "low").lower(), 0)


def _tier_at_least(alert: dict, min_tier: str | None) -> bool:
    if not min_tier:
        return True
    return _severity_rank(alert.get("severity")) >= _severity_rank(min_tier)


def _should_send(alert: dict, cfg: dict) -> tuple[bool, str]:
    reasons = cfg.get("include_reasons") or []
    reason = alert.get("alert_reason", "")
    if reasons and reason not in reasons:
        return False, f"reason {reason!r} not in include_reasons"
    if not _tier_at_least(alert, cfg.get("min_severity_tier")):
        return (False,
                f"severity {alert.get('severity')!r} below "
                f"min_severity_tier={cfg.get('min_severity_tier')!r}")
    return True, ""


# ---------------------------------------------------------------------------
# Formatting helpers.
# ---------------------------------------------------------------------------

# Sources that are conventionally rendered as percents in BDC disclosures.
# Anything else falls back to raw numeric formatting (ratios like D/E, NAV
# per share, etc.).
_PERCENT_SOURCE_SUFFIXES = (
    ".pct_change",
    ".asset_coverage_ratio",          # "1.50x" ≡ "150%" regulatory floor
    ".pik_income_ratio",
    ".fair_value_to_cost",
    ".dividend_coverage_nii_over_divs",
    ".debt_to_assets",
)


def _fmt_val(v, source: str | None = None) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and source and any(
                source.endswith(suf) for suf in _PERCENT_SOURCE_SUFFIXES):
            return f"{v * 100:.2f}%"
        return f"{v:.4f}" if isinstance(v, float) else str(v)
    return str(v)


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


def _format_triggers_html(alert: dict) -> str:
    rows: list[str] = []
    for t in alert.get("triggers") or []:
        src = t.get("source", "")
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(src))}</code></td>"
            f"<td style='text-align:right'>{escape(_fmt_val(t.get('value'), src))}</td>"
            f"<td style='text-align:center'>{escape(str(t.get('op', '')))}</td>"
            f"<td style='text-align:right'>{escape(_fmt_val(t.get('threshold'), src))}</td>"
            "</tr>"
        )
    return (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:13px'>"
        "<thead><tr style='background:#f6f6f6'>"
        "<th>Metric</th><th>Value</th><th>vs</th><th>Threshold</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _format_triggers_text(alert: dict) -> str:
    lines: list[str] = []
    for t in alert.get("triggers") or []:
        src = t.get("source")
        lines.append(
            f"  - {src}  "
            f"{_fmt_val(t.get('value'), src)} "
            f"{t.get('op')} "
            f"{_fmt_val(t.get('threshold'), src)}"
        )
    return "\n".join(lines) or "  (none)"


_SEVERITY_COLORS = {
    "critical": ("#991b1b", "#fde2e2"),
    "high":     ("#b91c1c", "#fee2e2"),
    "medium":   ("#b45309", "#fef3c7"),
    "low":      ("#15803d", "#dcfce7"),
    "info":     ("#1f2937", "#e5e7eb"),
}


def _severity_pill_html(sev: str | None) -> str:
    fg, bg = _SEVERITY_COLORS.get((sev or "low").lower(), _SEVERITY_COLORS["info"])
    label = (sev or "low").upper()
    return (
        f"<span style='background:{bg};color:{fg};padding:2px 8px;"
        f"border-radius:10px;font-size:11px;font-weight:600;"
        f"letter-spacing:0.5px'>{escape(label)}</span>"
    )


# ---------------------------------------------------------------------------
# Email builders.
# ---------------------------------------------------------------------------

def build_alert_email(alert: dict, cfg: dict, alert_path: Path) -> EmailMessage:
    bdc = alert.get("bdc") or {}
    ticker = bdc.get("ticker") or "?"
    company = bdc.get("company_name") or ticker
    cik = bdc.get("cik") or ""
    filing_end = bdc.get("as_of_filing_end") or ""

    scanned_at = (alert.get("scanned_at")
                  or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    prefix = (cfg.get("subject_prefix") or "[PriCredit]").strip()
    subject_parts = [
        prefix,
        (alert.get("severity") or "?").upper(),
        alert.get("alert_reason") or "alert",
        ticker,
    ]
    # Include score in the subject so a glance at the inbox tells the story.
    score = alert.get("composite_score")
    band = alert.get("band")
    if score is not None and band:
        subject_parts.append(f"score={score} ({band})")
    subject = " · ".join(p for p in subject_parts if p)

    text_body = "\n".join([
        f"PriCredit risk alert  {alert.get('alert_id', '?')}",
        f"case           : {alert.get('case_id', '?')}",
        f"severity       : {alert.get('severity')}",
        f"reason         : {alert.get('alert_reason')}",
        f"description    : {alert.get('description', '')}",
        "",
        f"BDC            : {ticker}  ({company})",
        f"CIK            : {cik}",
        f"as_of_filing   : {filing_end}",
        f"composite_score: {score}  band={band}",
        f"scanned_at     : {scanned_at}",
        "",
        "Triggers:",
        _format_triggers_text(alert),
        "",
        f"EDGAR filings  : {_edgar_filings_url(cik)}",
        "",
        f"Disclaimer: {alert.get('disclaimer', '')}",
        f"Source file: {alert_path}",
    ])

    edgar_url = _edgar_filings_url(cik)
    edgar_link = (f'<a href="{escape(edgar_url)}">{escape(edgar_url)}</a>'
                  if edgar_url else "—")

    html_body = f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222;margin:0;padding:16px;max-width:720px">
<h2 style="margin:0 0 4px 0">PriCredit risk alert &middot; {escape(ticker)}</h2>
<p style="margin:0 0 14px 0;color:#555">
  {_severity_pill_html(alert.get('severity'))}
  &nbsp;<b>{escape(alert.get('alert_reason', ''))}</b>
  &nbsp;&middot;&nbsp; {escape(alert.get('description', ''))}
</p>

<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;margin-bottom:16px;font-size:14px">
<tr><td style="color:#555">BDC</td><td><b>{escape(ticker)}</b> &middot; {escape(company)}</td></tr>
<tr><td style="color:#555">CIK</td><td><code>{escape(cik)}</code></td></tr>
<tr><td style="color:#555">As of filing</td><td>{escape(filing_end)}</td></tr>
<tr><td style="color:#555">Composite score</td><td><b>{escape(str(score))}</b> &middot; band <b>{escape(band or '—')}</b></td></tr>
<tr><td style="color:#555">Scanned at</td><td>{escape(scanned_at)}</td></tr>
<tr><td style="color:#555">Alert id</td><td><code>{escape(alert.get('alert_id', ''))}</code></td></tr>
<tr><td style="color:#555">Case id</td><td><code>{escape(alert.get('case_id', ''))}</code></td></tr>
<tr><td style="color:#555">EDGAR</td><td>{edgar_link}</td></tr>
</table>

<h3 style="margin:0 0 6px 0">Triggers</h3>
{_format_triggers_html(alert)}

<p style="margin-top:24px;color:#888;font-size:12px">
  {escape(alert.get('disclaimer', ''))}<br/>
  Source file: <code>{escape(str(alert_path))}</code>
</p>
</body></html>
"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    frm = cfg.get("from") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
    if not frm:
        raise RuntimeError("no From: set (configure email.from or SMTP_FROM / SMTP_USER)")
    msg["From"] = frm

    to_list = [a for a in (cfg.get("to") or []) if a]
    if not to_list:
        raise RuntimeError("notifications.yaml email.to is empty; nothing to do")
    msg["To"] = ", ".join(to_list)
    if cfg.get("cc"):
        msg["Cc"] = ", ".join(cfg["cc"])

    msg["Message-ID"] = make_msgid(domain="pricredit.local")
    msg["X-PriCredit-Alert-Id"] = alert.get("alert_id", "")
    msg["X-PriCredit-Case-Id"] = alert.get("case_id", "")
    msg["X-PriCredit-Ticker"] = ticker
    msg["X-PriCredit-Severity"] = alert.get("severity", "")

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if cfg.get("attach_json", True):
        raw = json.dumps(alert, indent=2, ensure_ascii=False).encode("utf-8")
        msg.add_attachment(
            raw,
            maintype="application",
            subtype="json",
            filename=alert_path.name,
        )
    return msg


def build_digest_email(alerts: list[tuple[dict, Path]],
                       risk_summary: dict | None,
                       cfg: dict,
                       reports_dir: Path) -> EmailMessage:
    """A single daily digest email instead of one-per-alert. Useful when
    the operator wants one email per morning regardless of alert volume."""
    prefix = (cfg.get("subject_prefix") or "[PriCredit]").strip()
    n = len(alerts)
    run_date = (risk_summary or {}).get("run_date") or reports_dir.name
    bands = (risk_summary or {}).get("band_counts") or {}
    band_str = ", ".join(f"{k}={v}" for k, v in sorted(bands.items()))
    subject = f"{prefix} Daily risk digest · {run_date} · {n} alerts"
    if band_str:
        subject += f" · {band_str}"

    rows_html: list[str] = []
    rows_txt: list[str] = []
    for alert, path in alerts:
        bdc = alert.get("bdc") or {}
        rows_html.append(
            "<tr>"
            f"<td>{_severity_pill_html(alert.get('severity'))}</td>"
            f"<td><b>{escape(bdc.get('ticker') or '?')}</b></td>"
            f"<td>{escape(alert.get('alert_reason', ''))}</td>"
            f"<td style='text-align:right'>{escape(str(alert.get('composite_score', '')))}</td>"
            f"<td>{escape(alert.get('band') or '')}</td>"
            f"<td style='color:#555'>{escape(alert.get('description', ''))}</td>"
            "</tr>"
        )
        rows_txt.append(
            f"  [{(alert.get('severity') or '').upper():<8}] "
            f"{(bdc.get('ticker') or '?'):<6} "
            f"{alert.get('alert_reason', '')}  "
            f"score={alert.get('composite_score')} ({alert.get('band')})  "
            f"— {alert.get('description', '')}"
        )

    html = f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222;padding:16px;max-width:880px">
<h2 style="margin:0 0 4px 0">PriCredit daily risk digest</h2>
<p style="margin:0 0 14px 0;color:#555">
  Run date <b>{escape(run_date)}</b> &middot; BDCs scored:
  <b>{escape(str((risk_summary or {}).get('n_bdcs_scored', '?')))}</b>
  &middot; alerts: <b>{n}</b>
  {('&middot; bands: <code>' + escape(band_str) + '</code>') if band_str else ''}
</p>

<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<thead><tr style="background:#f6f6f6">
<th>Severity</th><th>BDC</th><th>Reason</th>
<th>Score</th><th>Band</th><th>Description</th>
</tr></thead>
<tbody>{''.join(rows_html) or '<tr><td colspan="6" style="text-align:center;color:#888">no alerts today</td></tr>'}</tbody>
</table>

<p style="margin-top:20px;color:#888;font-size:12px">
  Reports directory: <code>{escape(str(reports_dir))}</code><br/>
  Heuristic, not investment advice. See docs/RISK_MODEL.md.
</p>
</body></html>
"""

    text = "\n".join([
        f"PriCredit daily risk digest — {run_date}",
        f"BDCs scored: {(risk_summary or {}).get('n_bdcs_scored', '?')}"
        f" · alerts: {n}"
        f"{(' · bands: ' + band_str) if band_str else ''}",
        "",
        *(rows_txt or ["  (no alerts today)"]),
        "",
        f"Reports directory: {reports_dir}",
        "Heuristic, not investment advice. See docs/RISK_MODEL.md.",
    ])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    frm = cfg.get("from") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
    if not frm:
        raise RuntimeError("no From: set (configure email.from or SMTP_FROM / SMTP_USER)")
    msg["From"] = frm
    to_list = [a for a in (cfg.get("to") or []) if a]
    if not to_list:
        raise RuntimeError("notifications.yaml email.to is empty; nothing to do")
    msg["To"] = ", ".join(to_list)
    if cfg.get("cc"):
        msg["Cc"] = ", ".join(cfg["cc"])
    msg["Message-ID"] = make_msgid(domain="pricredit.local")
    msg["X-PriCredit-Digest-Date"] = run_date
    msg["X-PriCredit-Alert-Count"] = str(n)

    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if cfg.get("attach_json", True) and risk_summary is not None:
        raw = json.dumps(risk_summary, indent=2, ensure_ascii=False).encode("utf-8")
        msg.add_attachment(raw, maintype="application", subtype="json",
                           filename="risk_summary.json")
    return msg


# ---------------------------------------------------------------------------
# SMTP.
# ---------------------------------------------------------------------------

def send_smtp(msg: EmailMessage, to_list: list[str]) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    use_ssl = os.environ.get("SMTP_USE_SSL", "0") == "1"
    use_starttls = os.environ.get("SMTP_STARTTLS", "1") == "1" and not use_ssl

    if not host:
        raise RuntimeError("SMTP_HOST not set; export SMTP_* in ~/.pricredit-env")

    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=30) as srv:
        srv.ehlo()
        if use_starttls:
            srv.starttls()
            srv.ehlo()
        if user and password:
            srv.login(user, password)
        srv.send_message(msg, to_addrs=to_list)


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def iter_alerts(args) -> list[Path]:
    paths: list[Path] = []
    for p in args.alert or []:
        paths.append(Path(p))
    if args.reports_dir:
        root = Path(args.reports_dir)
        paths.extend(sorted(root.glob("alert_RISK-*.json")))
    # De-dup while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)
    return unique


def sent_marker_path(alert_path: Path) -> Path:
    return alert_path.parent / ".sent" / (alert_path.stem + ".json")


def _dry_run_dump(msg: EmailMessage, recipients: list[str], label: str) -> None:
    print(f"--- DRY RUN: {label} -> {', '.join(recipients)} ---", file=sys.stderr)
    print(f"Subject: {msg['Subject']}")
    print(f"From:    {msg['From']}")
    print(f"To:      {msg['To']}")
    if msg.get("Cc"):
        print(f"Cc:      {msg['Cc']}")
    print()
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            print(part.get_content())
            break
    print()


def _load_cfg(path: Path) -> dict:
    if not path.exists():
        return {}
    full = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (full.get("email") or {}) if isinstance(full, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="PriCredit risk alert dispatcher")
    ap.add_argument("--config", default="ingest/notifications.yaml")
    ap.add_argument("--reports-dir",
                    help="Directory whose alert_RISK-*.json should be sent.")
    ap.add_argument("--alert", action="append", default=[],
                    help="Explicit alert JSON path (repeatable).")
    ap.add_argument("--to",
                    help="Override recipients, comma-separated.")
    ap.add_argument("--digest", action="store_true",
                    help="Send one digest email instead of one per alert.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compose only, do not open SMTP.")
    ap.add_argument("--force", action="store_true",
                    help="Re-send even if .sent marker exists.")
    args = ap.parse_args()

    cfg = _load_cfg(Path(args.config))
    if args.to:
        cfg["to"] = [x.strip() for x in args.to.split(",") if x.strip()]

    paths = iter_alerts(args)
    if not paths and not args.digest:
        print("[send_risk_alerts] no alert files matched", file=sys.stderr)
        return 0

    # Load and pre-filter. The digest path still needs filtered alerts.
    loaded: list[tuple[dict, Path]] = []
    for p in paths:
        try:
            alert = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[send_risk_alerts] skip {p}: {exc}", file=sys.stderr)
            continue
        ok, why = _should_send(alert, cfg)
        if not ok:
            print(f"[send_risk_alerts] skip {p.name}: {why}", file=sys.stderr)
            continue
        loaded.append((alert, p))

    # ---- digest branch ----
    if args.digest:
        reports_dir = Path(args.reports_dir) if args.reports_dir else (
            paths[0].parent if paths else Path("."))
        summary_path = reports_dir / "risk_summary.json"
        risk_summary = None
        if summary_path.exists():
            try:
                risk_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[send_risk_alerts] could not read {summary_path}: {exc}",
                      file=sys.stderr)

        digest_marker = reports_dir / ".sent" / "digest.json"
        if digest_marker.exists() and not args.force:
            print(f"[send_risk_alerts] digest already sent -> {digest_marker}",
                  file=sys.stderr)
            return 0

        try:
            msg = build_digest_email(loaded, risk_summary, cfg, reports_dir)
        except Exception as exc:
            print(f"[send_risk_alerts] digest build failed: {exc}", file=sys.stderr)
            return 1

        recipients = (list(cfg.get("to") or [])
                      + list(cfg.get("cc") or [])
                      + list(cfg.get("bcc") or []))
        if args.dry_run:
            _dry_run_dump(msg, recipients, f"digest({len(loaded)} alerts)")
            return 0
        try:
            send_smtp(msg, recipients)
        except Exception as exc:
            print(f"[send_risk_alerts] digest SMTP failed: {exc}", file=sys.stderr)
            return 1

        digest_marker.parent.mkdir(parents=True, exist_ok=True)
        digest_marker.write_text(json.dumps({
            "kind": "digest",
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recipients": recipients,
            "alert_count": len(loaded),
            "subject": msg["Subject"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[send_risk_alerts] digest sent "
              f"({len(loaded)} alerts) -> {', '.join(recipients)}",
              file=sys.stderr)
        return 0

    # ---- per-alert branch ----
    sent = 0
    skipped = 0
    for alert, p in loaded:
        marker = sent_marker_path(p)
        if marker.exists() and not args.force:
            print(f"[send_risk_alerts] skip {p.name}: already sent -> {marker}",
                  file=sys.stderr)
            skipped += 1
            continue
        try:
            msg = build_alert_email(alert, cfg, p)
        except Exception as exc:
            print(f"[send_risk_alerts] build failed for {p.name}: {exc}",
                  file=sys.stderr)
            skipped += 1
            continue

        recipients = (list(cfg.get("to") or [])
                      + list(cfg.get("cc") or [])
                      + list(cfg.get("bcc") or []))

        if args.dry_run:
            _dry_run_dump(msg, recipients, p.name)
            continue

        try:
            send_smtp(msg, recipients)
        except Exception as exc:
            print(f"[send_risk_alerts] SMTP failed for {p.name}: {exc}",
                  file=sys.stderr)
            skipped += 1
            continue

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "alert_id": alert.get("alert_id"),
            "case_id": alert.get("case_id"),
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recipients": recipients,
            "subject": msg["Subject"],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[send_risk_alerts] sent {p.name} -> {', '.join(recipients)}",
              file=sys.stderr)
        sent += 1

    print(f"[send_risk_alerts] done: sent={sent} skipped={skipped} "
          f"considered={len(loaded)} total_paths={len(paths)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
