#!/usr/bin/env python3
"""
PriCredit — Risk alert dispatcher (WhatsApp via OpenClaw).

Shells out to the OpenClaw CLI so we don't have to speak the local
WebSocket gateway protocol directly. One call per recipient per alert:

    openclaw message send --channel whatsapp \\
        --target +1...  --message "..." --json

Credentials + session live in ~/.openclaw/openclaw.json (channels.whatsapp);
this script only reads non-sensitive routing from
ingest/notifications.yaml (whatsapp.to, min_severity_tier, ...).

Idempotency: for each alert we write
    reports/<DATE>/.sent/<alert_id>.whatsapp.json
once the CLI returns 0, so reruns skip it. Digest uses a separate
marker at reports/<DATE>/.sent/digest.whatsapp.json.

Usage:
    scripts/send_whatsapp_alerts.py --reports-dir reports/2026-04-18
    scripts/send_whatsapp_alerts.py --reports-dir reports/2026-04-18 --dry-run
    scripts/send_whatsapp_alerts.py --reports-dir reports/2026-04-18 --digest
    scripts/send_whatsapp_alerts.py --alert reports/2026-04-18/alert_RISK-...json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pyyaml required: pip install -r scripts/requirements.txt") from exc


# Must match the `bands` order in ingest/risk_weights.yaml. Shared with
# the email path but duplicated here so neither script imports the
# other (they're independent command-line tools).
TIER_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Per-severity emoji prefix. Keeps the single-line preview on the
# phone instantly readable without scrolling. Chosen to look right on
# iOS/Android WhatsApp — avoid icons that render as "?" on older builds.
SEVERITY_EMOJI = {
    "critical": "🛑",
    "high":     "🔴",
    "medium":   "🟠",
    "low":      "🟡",
    "info":     "🔵",
}


# ---------------------------------------------------------------------------
# Filtering (mirrors send_risk_alerts.py).
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
# Formatting — must match the "WhatsApp looks right on a phone" contract.
# ---------------------------------------------------------------------------

_PERCENT_SOURCE_SUFFIXES = (
    ".pct_change",
    ".asset_coverage_ratio",
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


def _format_triggers_short(alert: dict, max_lines: int = 4) -> str:
    """A compact, chat-friendly trigger list. Long triggers get trimmed
    so the message still fits in a WhatsApp preview."""
    lines: list[str] = []
    for t in (alert.get("triggers") or [])[:max_lines]:
        src = t.get("source") or ""
        lines.append(
            f"• {src} {_fmt_val(t.get('value'), src)} "
            f"{t.get('op', '')} {_fmt_val(t.get('threshold'), src)}"
        )
    remaining = len(alert.get("triggers") or []) - max_lines
    if remaining > 0:
        lines.append(f"• …+{remaining} more")
    return "\n".join(lines) or "• (none)"


def build_alert_text(alert: dict, cfg: dict) -> str:
    """Compose the single-alert WhatsApp body.

    Target shape (≤ ~700 chars so it fits in WhatsApp's free-text
    preview on the lock screen and doesn't get collapsed):

        🏦 PriCredit · 🔴 HIGH · ARCC
        leverage_elevated · score 72 (high)
        Asset coverage 1.55 below 1.58 guardrail
        Triggers:
        • cf.asset_coverage_ratio 1.5500 <= 1.5800
        • cf.debt_to_assets 0.5800 >= 0.5500
        score 72 (high) · as_of 2025-12-31
        id: RISK-ARCC-20250101-abc123
        EDGAR: https://...
    """
    bdc = alert.get("bdc") or {}
    ticker = bdc.get("ticker") or "?"
    company = bdc.get("company_name") or ticker
    filing_end = bdc.get("as_of_filing_end") or ""
    cik = bdc.get("cik") or ""

    sev = (alert.get("severity") or "low").lower()
    emoji = SEVERITY_EMOJI.get(sev, "")
    prefix = (cfg.get("subject_prefix") or "🏦 PriCredit").strip()
    score = alert.get("composite_score")
    band = alert.get("band") or ""

    header = (f"{prefix} · {emoji} {sev.upper()} · {ticker}"
              if emoji else f"{prefix} · {sev.upper()} · {ticker}")
    line2 = f"{alert.get('alert_reason') or 'alert'}"
    if score is not None:
        line2 += f" · score {score}"
        if band:
            line2 += f" ({band})"

    body_lines = [
        header,
        line2,
    ]
    desc = alert.get("description")
    if desc:
        body_lines.append(desc)
    body_lines.append("Triggers:")
    body_lines.append(_format_triggers_short(alert))
    if company and company != ticker:
        body_lines.append(f"{company} · as_of {filing_end}".rstrip(" · "))
    body_lines.append(f"id: {alert.get('alert_id', '')}")
    url = _edgar_filings_url(cik)
    if url:
        body_lines.append(f"EDGAR: {url}")
    return "\n".join(body_lines)


def build_digest_text(alerts: list[tuple[dict, Path]],
                      risk_summary: dict | None,
                      cfg: dict,
                      run_date: str) -> str:
    """One summary message, ranked by severity → score. Capped at 15
    lines so the message stays under WhatsApp's soft 4096-char limit
    even with a universe-wide alert storm."""
    prefix = (cfg.get("subject_prefix") or "🏦 PriCredit").strip()
    n = len(alerts)

    # Rank within the chat preview: critical first, then by composite score desc.
    ranked = sorted(
        alerts,
        key=lambda ap: (
            -_severity_rank(ap[0].get("severity")),
            -(ap[0].get("composite_score") or 0),
        ),
    )

    header = f"{prefix} digest · {run_date} · {n} alert{'s' if n != 1 else ''}"
    bands = (risk_summary or {}).get("band_counts") or {}
    band_str = " ".join(f"{k}={v}" for k, v in sorted(bands.items()))
    n_scored = (risk_summary or {}).get("n_bdcs_scored")
    subhead_bits = []
    if n_scored is not None:
        subhead_bits.append(f"scored {n_scored}")
    if band_str:
        subhead_bits.append(f"bands {band_str}")
    subhead = " · ".join(subhead_bits)

    lines = [header]
    if subhead:
        lines.append(subhead)

    top = ranked[:15]
    for alert, _ in top:
        bdc = alert.get("bdc") or {}
        sev = (alert.get("severity") or "low").lower()
        emoji = SEVERITY_EMOJI.get(sev, "")
        lines.append(
            f"{emoji} {(bdc.get('ticker') or '?'):<6} "
            f"{alert.get('alert_reason', '')} · "
            f"score {alert.get('composite_score')} "
            f"({alert.get('band') or '?'})"
        )
    if len(ranked) > len(top):
        lines.append(f"… +{len(ranked) - len(top)} more")
    lines.append(f"Report: reports/{run_date}/briefs/index.md")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenClaw CLI bridge.
# ---------------------------------------------------------------------------

def _openclaw_bin(cfg: dict) -> str:
    """Resolve the openclaw binary. Priority:
        1. env OPENCLAW_CMD
        2. notifications.yaml whatsapp.command
        3. "openclaw" on PATH
    """
    cand = os.environ.get("OPENCLAW_CMD") or cfg.get("command") or "openclaw"
    # If it's a bare name, check it's actually on PATH so we fail loudly
    # here instead of deep inside subprocess with a vague error.
    if "/" not in cand and shutil.which(cand) is None:
        raise RuntimeError(
            f"openclaw CLI not found (looked for {cand!r} on PATH). "
            "Install OpenClaw or set OPENCLAW_CMD to the full binary path.")
    return cand


def _dry_run_preview(text: str, to: str, label: str) -> None:
    """Print the composed WhatsApp payload locally. Invoked in place of
    the openclaw CLI so the operator sees exactly what would go out —
    no subprocess, no gateway roundtrip, no guessing.

    Gracefully bail on BrokenPipeError — common when the operator
    pipes us through `| head -N` or similar. Once the downstream
    reader closes, there's nothing useful left to do, so we exit 0
    instead of polluting the log with a bogus failure line.
    """
    banner = f"--- DRY RUN · {label} -> WhatsApp {to} ---"
    try:
        print(banner, file=sys.stderr)
        print(text)
        print("-" * len(banner), file=sys.stderr)
    except BrokenPipeError:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        # Detach stdout so any subsequent prints (including
        # interpreter-shutdown flushes) don't retrigger the error.
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
        raise SystemExit(0)


def send_whatsapp(text: str, to: str, cfg: dict, *, dry_run: bool = False,
                  verbose: bool = False, label: str = "") -> dict:
    """Invoke the OpenClaw CLI once. Returns the parsed JSON response
    (CLI output with --json) or, on failure, raises.

    In dry-run mode we skip the CLI entirely and preview the payload
    locally — faster feedback and avoids spinning up openclaw's node
    process just to throw the result away.

    Kept synchronous on purpose: alert volume is low (tens/day), and a
    linear loop makes the idempotency marker bookkeeping trivial. If
    this ever has to scale, batch via asyncio, not threads — the CLI
    itself spawns a short-lived node process per call.
    """
    if dry_run:
        _dry_run_preview(text, to, label or "alert")
        return {"ok": True, "dry_run": True}

    cmd = [
        _openclaw_bin(cfg),
        "message", "send",
        "--channel", "whatsapp",
        "--target", to,
        "--message", text,
        "--json",
    ]
    if verbose:
        cmd.append("--verbose")

    timeout = float(cfg.get("timeout_s") or 30)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"openclaw timed out after {timeout}s") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"openclaw exited {proc.returncode}: "
            f"{stderr or stdout or '<no output>'}"
        )

    # --json emits a single object on stdout. Be defensive: some older
    # builds print a banner line before the JSON. Try JSON-only first,
    # fall back to parsing the last {...} block.
    try:
        return json.loads(stdout) if stdout else {"ok": True}
    except json.JSONDecodeError:
        start = stdout.rfind("{")
        if start >= 0:
            try:
                return json.loads(stdout[start:])
            except json.JSONDecodeError:
                pass
        return {"ok": True, "raw": stdout, "stderr": stderr}


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def _load_cfg(path: Path) -> dict:
    if not path.exists():
        return {}
    full = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (full.get("whatsapp") or {}) if isinstance(full, dict) else {}


def iter_alerts(args) -> list[Path]:
    paths: list[Path] = []
    for p in args.alert or []:
        paths.append(Path(p))
    if args.reports_dir:
        root = Path(args.reports_dir)
        paths.extend(sorted(root.glob("alert_RISK-*.json")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)
    return unique


def sent_marker_path(alert_path: Path) -> Path:
    """Per-alert WhatsApp marker. Sits alongside the email marker
    (<stem>.json) but with a `.whatsapp.json` suffix so both channels
    can independently gate resends."""
    return alert_path.parent / ".sent" / (alert_path.stem + ".whatsapp.json")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PriCredit risk alert dispatcher — WhatsApp via OpenClaw")
    ap.add_argument("--config", default="ingest/notifications.yaml")
    ap.add_argument("--reports-dir",
                    help="Directory whose alert_RISK-*.json should be sent.")
    ap.add_argument("--alert", action="append", default=[],
                    help="Explicit alert JSON path (repeatable).")
    ap.add_argument("--to",
                    help="Override recipients, comma-separated E.164 numbers.")
    ap.add_argument("--digest", action="store_true",
                    help="Send one digest message instead of one per alert.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compose only; pass --dry-run to openclaw so nothing "
                         "leaves the machine.")
    ap.add_argument("--force", action="store_true",
                    help="Re-send even if .whatsapp.json marker exists.")
    ap.add_argument("--verbose", action="store_true",
                    help="Verbose openclaw output on stderr.")
    args = ap.parse_args()

    cfg = _load_cfg(Path(args.config))
    if args.to:
        cfg["to"] = [x.strip() for x in args.to.split(",") if x.strip()]

    if not cfg.get("enabled", False) and not args.to:
        print("[send_whatsapp_alerts] whatsapp disabled in "
              f"{args.config} (set whatsapp.enabled: true or pass --to). "
              "no-op.", file=sys.stderr)
        return 0

    recipients = [n for n in (cfg.get("to") or []) if n]
    if not recipients:
        print("[send_whatsapp_alerts] notifications.yaml whatsapp.to is "
              "empty; nothing to do", file=sys.stderr)
        return 0

    paths = iter_alerts(args)
    if not paths and not args.digest:
        print("[send_whatsapp_alerts] no alert files matched", file=sys.stderr)
        return 0

    # Load + severity filter. Digest branch still uses the filtered list.
    loaded: list[tuple[dict, Path]] = []
    for p in paths:
        try:
            alert = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[send_whatsapp_alerts] skip {p}: {exc}", file=sys.stderr)
            continue
        ok, why = _should_send(alert, cfg)
        if not ok:
            print(f"[send_whatsapp_alerts] skip {p.name}: {why}",
                  file=sys.stderr)
            continue
        loaded.append((alert, p))

    # ---- digest branch ----
    if args.digest:
        reports_dir = Path(args.reports_dir) if args.reports_dir else (
            paths[0].parent if paths else Path("."))
        run_date = reports_dir.name
        summary_path = reports_dir / "risk_summary.json"
        risk_summary = None
        if summary_path.exists():
            try:
                risk_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[send_whatsapp_alerts] could not read "
                      f"{summary_path}: {exc}", file=sys.stderr)

        marker = reports_dir / ".sent" / "digest.whatsapp.json"
        if marker.exists() and not args.force:
            print(f"[send_whatsapp_alerts] digest already sent -> {marker}",
                  file=sys.stderr)
            return 0

        text = build_digest_text(loaded, risk_summary, cfg, run_date)

        sent_to: list[str] = []
        for to in recipients:
            try:
                send_whatsapp(text, to, cfg, dry_run=args.dry_run,
                              verbose=args.verbose,
                              label=f"digest({len(loaded)} alerts)")
            except Exception as exc:
                print(f"[send_whatsapp_alerts] digest -> {to} failed: {exc}",
                      file=sys.stderr)
                continue
            sent_to.append(to)
            print(f"[send_whatsapp_alerts] digest sent -> {to}",
                  file=sys.stderr)

        if not args.dry_run and sent_to:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({
                "kind": "digest",
                "channel": "whatsapp",
                "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "recipients": sent_to,
                "alert_count": len(loaded),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0 if sent_to or args.dry_run else 1

    # ---- per-alert branch ----
    sent = 0
    previewed = 0
    skipped = 0
    for alert, p in loaded:
        marker = sent_marker_path(p)
        if marker.exists() and not args.force and not args.dry_run:
            print(f"[send_whatsapp_alerts] skip {p.name}: already sent -> "
                  f"{marker}", file=sys.stderr)
            skipped += 1
            continue

        text = build_alert_text(alert, cfg)

        # One recipient per call is the OpenClaw CLI contract. We loop
        # and record the last-error so partial success still writes a
        # marker for the numbers that did receive the message.
        ok_to: list[str] = []
        last_err: str | None = None
        verb = "preview" if args.dry_run else "sent"
        for to in recipients:
            try:
                send_whatsapp(text, to, cfg, dry_run=args.dry_run,
                              verbose=args.verbose, label=p.name)
            except Exception as exc:
                last_err = str(exc)
                print(f"[send_whatsapp_alerts] {p.name} -> {to} failed: {exc}",
                      file=sys.stderr)
                continue
            ok_to.append(to)
            print(f"[send_whatsapp_alerts] {verb} {p.name} -> {to}",
                  file=sys.stderr)

        if args.dry_run:
            if ok_to:
                previewed += 1
            continue

        if not ok_to:
            skipped += 1
            continue

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "alert_id": alert.get("alert_id"),
            "case_id": alert.get("case_id"),
            "channel": "whatsapp",
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recipients": ok_to,
            "partial_failure": last_err,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        sent += 1

    if args.dry_run:
        print(f"[send_whatsapp_alerts] done (dry-run): previewed={previewed} "
              f"considered={len(loaded)} total_paths={len(paths)}",
              file=sys.stderr)
    else:
        print(f"[send_whatsapp_alerts] done: sent={sent} skipped={skipped} "
              f"considered={len(loaded)} total_paths={len(paths)}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
