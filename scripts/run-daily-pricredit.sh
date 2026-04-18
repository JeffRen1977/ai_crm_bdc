#!/usr/bin/env bash
# =============================================================================
# PriCredit / AI-CRM — daily ingestion orchestrator.
#
# Pipeline:
#   1. Refresh the BDC universe if stale.
#   2. Download recent 10-K / 10-Q / 8-K for each BDC.
#   3. Parse XBRL company facts -> canonical metrics (NAV, NII, leverage,
#      asset coverage, PIK ratio, fair-value-to-cost, distributions).
#   4. Score each BDC with the risk engine and emit alert_*.json for
#      breaches (leverage, NAV decline, dividend coverage, PIK, etc.).
#   5. [opt-in] Email the day's alerts via scripts/send_risk_alerts.py.
#
# Schedule-of-Investments parsing and investor-report generation land in
# future commits (extract_portfolio.py, build_investor_report.py).
#
# Usage:
#   scripts/run-daily-pricredit.sh
#   scripts/run-daily-pricredit.sh --tickers ARCC,MAIN,OBDC
#   scripts/run-daily-pricredit.sh --skip-parse               # ingest only
#   scripts/run-daily-pricredit.sh --skip-risk                # ingest+parse, no scoring
#   scripts/run-daily-pricredit.sh --send-alerts              # + email alerts
#   scripts/run-daily-pricredit.sh --send-alerts --digest     # + one digest email
#   FORMS=10-K,10-Q LIMIT_PER_FORM=2 scripts/run-daily-pricredit.sh
#
# Environment:
#   PRICREDIT_UA_EMAIL    REQUIRED. Real contact email for EDGAR UA.
#   FORMS                 Default: 10-K,10-Q,8-K
#   LIMIT_PER_FORM        Default: 4
#   PUBLIC_ONLY           "1" to skip non-traded BDCs (default: 1)
#   UNIVERSE_MAX_AGE_H    Refresh bdc_universe.json if older (default: 168h = 7d)
#   SKIP_PARSE            "1" to skip the XBRL parse step (default: 0)
#   SKIP_RISK             "1" to skip the risk scoring step (default: 0)
#   SEND_ALERTS           "1" to email alerts after scoring (default: 0)
#   SEND_DIGEST           "1" to send one digest email instead of one-per-alert
#   ALERT_DRY_RUN         "1" to dry-run the dispatcher (no SMTP)
#   RISK_WEIGHTS          Path to ingest/risk_weights.yaml (default: repo copy)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [[ -f "$HOME/.pricredit-env" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.pricredit-env"
fi

PY="${PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

FORMS="${FORMS:-10-K,10-Q,8-K}"
LIMIT_PER_FORM="${LIMIT_PER_FORM:-4}"
PUBLIC_ONLY="${PUBLIC_ONLY:-1}"
UNIVERSE_MAX_AGE_H="${UNIVERSE_MAX_AGE_H:-168}"
SKIP_PARSE="${SKIP_PARSE:-0}"
SKIP_RISK="${SKIP_RISK:-0}"
SEND_ALERTS="${SEND_ALERTS:-0}"
SEND_DIGEST="${SEND_DIGEST:-0}"
ALERT_DRY_RUN="${ALERT_DRY_RUN:-0}"
RISK_WEIGHTS="${RISK_WEIGHTS:-$ROOT/ingest/risk_weights.yaml}"

# Flag parsing — anything not recognized here is forwarded to fetch_filings.py.
fetch_passthrough=()
for arg in "$@"; do
  case "$arg" in
    --skip-parse)     SKIP_PARSE=1 ;;
    --no-skip-parse)  SKIP_PARSE=0 ;;
    --skip-risk)      SKIP_RISK=1 ;;
    --no-skip-risk)   SKIP_RISK=0 ;;
    --send-alerts)    SEND_ALERTS=1 ;;
    --no-send-alerts) SEND_ALERTS=0 ;;
    --digest)         SEND_DIGEST=1 ;;
    --alert-dry-run)  ALERT_DRY_RUN=1 ;;
    *)                fetch_passthrough+=("$arg") ;;
  esac
done

DATE="$(date -u +%Y-%m-%d)"
LOG_DIR="$ROOT/reports/$DATE"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/pricredit.log"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }

log "PriCredit daily run (date=$DATE forms=$FORMS limit_per_form=$LIMIT_PER_FORM)"

# -----------------------------------------------------------------------------
# 0. Preflight: EDGAR UA must be set.
# -----------------------------------------------------------------------------
if ! "$PY" "$SCRIPT_DIR/_edgar_common.py" >>"$LOG" 2>&1; then
  log "FATAL: EDGAR preflight failed; see $LOG"
  exit 3
fi

# -----------------------------------------------------------------------------
# 1. BDC universe freshness.
# -----------------------------------------------------------------------------
UNIVERSE="$ROOT/bdc/bdc_universe.json"
needs_refresh=0
if [[ ! -s "$UNIVERSE" ]]; then
  needs_refresh=1
elif [[ -n "$(find "$UNIVERSE" -mmin "+$((UNIVERSE_MAX_AGE_H * 60))" 2>/dev/null)" ]]; then
  needs_refresh=1
fi

if [[ "$needs_refresh" == "1" ]]; then
  log "refreshing BDC universe -> $UNIVERSE"
  "$PY" "$SCRIPT_DIR/discover_bdcs.py" --out "$UNIVERSE" >>"$LOG" 2>&1
else
  log "BDC universe is fresh (< ${UNIVERSE_MAX_AGE_H}h)"
fi

# -----------------------------------------------------------------------------
# 2. Fetch recent filings.
# -----------------------------------------------------------------------------
extra_args=()
[[ "$PUBLIC_ONLY" == "1" ]] && extra_args+=("--public-only")

fetch_cmd=("$PY" "$SCRIPT_DIR/fetch_filings.py"
           --universe "$UNIVERSE"
           --out "$ROOT/filings"
           --forms "$FORMS"
           --limit-per-form "$LIMIT_PER_FORM"
           "${extra_args[@]}"
           "${fetch_passthrough[@]}")
log "fetching filings: ${fetch_cmd[*]}"
"${fetch_cmd[@]}" >>"$LOG" 2>&1 || log "fetch_filings.py exited non-zero (continuing)"

# -----------------------------------------------------------------------------
# 3. Parse XBRL company facts -> canonical metrics.
# -----------------------------------------------------------------------------

if [[ "$SKIP_PARSE" != "1" ]]; then
  parse_args=(--universe "$UNIVERSE"
              --out "$ROOT/extracted"
              --run-summary "$LOG_DIR/parse_summary.json")
  [[ "$PUBLIC_ONLY" == "1" ]] || parse_args+=("--include-private")

  # Reuse any --tickers / --ciks the operator passed to fetch_filings.py so
  # both steps stay in sync.
  for arg in "${fetch_passthrough[@]}"; do
    case "$arg" in
      --tickers|--ciks|--max-bdcs|--ticker)
        parse_args+=("$arg") ;;
      -*) ;;    # drop flags the parser doesn't know
      *)  parse_args+=("$arg") ;;
    esac
  done

  log "parsing XBRL facts: parse_filings.py ${parse_args[*]}"
  "$PY" "$SCRIPT_DIR/parse_filings.py" "${parse_args[@]}" >>"$LOG" 2>&1 \
    || log "parse_filings.py exited non-zero (continuing)"
else
  log "SKIP_PARSE=1 -> skipping XBRL parse"
fi

# -----------------------------------------------------------------------------
# 4. Score each BDC with the risk engine and emit alert_*.json files.
# -----------------------------------------------------------------------------
if [[ "$SKIP_RISK" != "1" && "$SKIP_PARSE" != "1" ]]; then
  risk_args=(--weights "$RISK_WEIGHTS"
             --extracted "$ROOT/extracted"
             --reports "$ROOT/reports"
             --universe "$UNIVERSE"
             --date "$DATE"
             --force)
  for arg in "${fetch_passthrough[@]}"; do
    case "$arg" in
      --tickers|--ciks|--ticker)
        risk_args+=("$arg") ;;
      -*) ;;
      *)  risk_args+=("$arg") ;;
    esac
  done

  log "scoring risk: compute_risk.py ${risk_args[*]}"
  "$PY" "$SCRIPT_DIR/compute_risk.py" "${risk_args[@]}" >>"$LOG" 2>&1 \
    || log "compute_risk.py exited non-zero (continuing)"
elif [[ "$SKIP_RISK" == "1" ]]; then
  log "SKIP_RISK=1 -> skipping risk scoring"
else
  log "parse step skipped -> risk scoring also skipped (needs extracted/*/facts/summary.json)"
fi

# -----------------------------------------------------------------------------
# 5. [opt-in] Email the day's risk alerts.
# -----------------------------------------------------------------------------
if [[ "$SEND_ALERTS" == "1" ]]; then
  alert_count=$(find "$LOG_DIR" -maxdepth 1 -name 'alert_*.json' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$alert_count" == "0" ]]; then
    log "no alert_*.json in $LOG_DIR; skipping dispatcher"
  else
    dispatch_args=(--config "$ROOT/ingest/notifications.yaml"
                   --reports-dir "$LOG_DIR")
    [[ "$SEND_DIGEST" == "1" ]]   && dispatch_args+=("--digest")
    [[ "$ALERT_DRY_RUN" == "1" ]] && dispatch_args+=("--dry-run")

    log "dispatching $alert_count alerts (digest=$SEND_DIGEST dry_run=$ALERT_DRY_RUN)"
    "$PY" "$SCRIPT_DIR/send_risk_alerts.py" "${dispatch_args[@]}" >>"$LOG" 2>&1 \
      || log "send_risk_alerts.py exited non-zero (continuing)"
  fi
elif [[ "$SEND_DIGEST" == "1" ]]; then
  log "NOTE: --digest requires --send-alerts; digest not dispatched"
fi

# -----------------------------------------------------------------------------
# 6. Summary.
# -----------------------------------------------------------------------------
total_filings=$(find "$ROOT/filings" -maxdepth 3 -name meta.json 2>/dev/null | wc -l | tr -d ' ')
fresh_today=$(find "$ROOT/filings" -maxdepth 3 -name meta.json -newermt "$DATE" 2>/dev/null | wc -l | tr -d ' ')
total_parsed=$(find "$ROOT/extracted" -maxdepth 3 -name summary.json 2>/dev/null | wc -l | tr -d ' ')
total_scored=$(find "$LOG_DIR" -maxdepth 1 -name 'risk_*.json' ! -name 'risk_summary.json' 2>/dev/null | wc -l | tr -d ' ')
total_alerts=$(find "$LOG_DIR" -maxdepth 1 -name 'alert_*.json' 2>/dev/null | wc -l | tr -d ' ')
total_sent=0
if [[ -d "$LOG_DIR/.sent" ]]; then
  total_sent=$(find "$LOG_DIR/.sent" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
fi
log "filings on disk: total=$total_filings fetched_today=$fresh_today"
log "BDCs parsed: $total_parsed (see $LOG_DIR/parse_summary.json)"
log "BDCs scored: $total_scored; risk alerts emitted: $total_alerts; dispatched: $total_sent"
log "PriCredit daily run done. Log: $LOG"
