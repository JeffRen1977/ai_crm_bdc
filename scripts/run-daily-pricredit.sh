#!/usr/bin/env bash
# =============================================================================
# PriCredit / AI-CRM — daily ingestion orchestrator (v0).
#
# Current scope: refresh the BDC universe if stale, then pull recent
# 10-K / 10-Q / 8-K for each BDC. Parsing + risk modeling land in future
# commits under scripts/parse_filings.py, scripts/compute_risk.py, etc.
#
# Usage:
#   scripts/run-daily-pricredit.sh
#   scripts/run-daily-pricredit.sh --tickers ARCC,MAIN,OBDC
#   FORMS=10-K,10-Q LIMIT_PER_FORM=2 scripts/run-daily-pricredit.sh
#
# Environment:
#   PRICREDIT_UA_EMAIL    REQUIRED. Real contact email for EDGAR UA.
#   FORMS                 Default: 10-K,10-Q,8-K
#   LIMIT_PER_FORM        Default: 4
#   PUBLIC_ONLY           "1" to skip non-traded BDCs (default: 1)
#   UNIVERSE_MAX_AGE_H    Refresh bdc_universe.json if older (default: 168h = 7d)
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
           "$@")
log "fetching filings: ${fetch_cmd[*]}"
"${fetch_cmd[@]}" >>"$LOG" 2>&1 || log "fetch_filings.py exited non-zero (continuing)"

# -----------------------------------------------------------------------------
# 3. Summary.
# -----------------------------------------------------------------------------
total_filings=$(find "$ROOT/filings" -maxdepth 3 -name meta.json 2>/dev/null | wc -l | tr -d ' ')
fresh_today=$(find "$ROOT/filings" -maxdepth 3 -name meta.json -newermt "$DATE" 2>/dev/null | wc -l | tr -d ' ')
log "filings on disk: total=$total_filings fetched_today=$fresh_today"
log "PriCredit daily run done. Log: $LOG"
