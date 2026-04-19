#!/usr/bin/env bash
# =============================================================================
# PriCredit — send today's alert_RISK-*.json files to the WhatsApp recipients
# configured in ingest/notifications.yaml (whatsapp.to) via OpenClaw.
#
# Usage:
#   scripts/send-whatsapp-alerts.sh                       # today
#   scripts/send-whatsapp-alerts.sh 2026-04-18            # specific date
#   scripts/send-whatsapp-alerts.sh 2026-04-18 --dry-run
#   scripts/send-whatsapp-alerts.sh 2026-04-18 --digest   # one summary msg
#   scripts/send-whatsapp-alerts.sh --to +18586039367 reports/2026-04-18
#
# A trailing "reports/YYYY-MM-DD[/]" argument is treated as the reports dir.
# Otherwise the first positional matching YYYY-MM-DD is expanded to
# reports/<date>. All other args pass through to send_whatsapp_alerts.py.
#
# Prerequisites:
#   - OpenClaw installed and WhatsApp linked (`openclaw channels login
#     --channel whatsapp`).
#   - Recipients in `ingest/notifications.yaml` (whatsapp.to) are present
#     in ~/.openclaw/openclaw.json channels.whatsapp.allowFrom.
#   - Set `whatsapp.enabled: true` in ingest/notifications.yaml, or pass
#     --to on the command line to bypass the gate.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

if [[ -f "$HOME/.pricredit-env" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.pricredit-env"
fi

# Verify the openclaw CLI is reachable before invoking Python — gives
# a clearer error than "subprocess failed".
if ! command -v "${OPENCLAW_CMD:-openclaw}" >/dev/null 2>&1; then
  echo "ERROR: openclaw CLI not found on PATH. Install OpenClaw or set"\
       "OPENCLAW_CMD to the full binary path." >&2
  exit 127
fi

date_or_dir=""
pass_through=()
prev=""
for arg in "$@"; do
  case "$prev" in
    --alert|--to|--config|--reports-dir)
      pass_through+=("$arg"); prev="$arg"; continue ;;
  esac
  case "$arg" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      date_or_dir="--reports-dir reports/$arg" ;;
    reports/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|reports/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/)
      date_or_dir="--reports-dir ${arg%/}" ;;
    *)
      pass_through+=("$arg") ;;
  esac
  prev="$arg"
done

if [[ -z "$date_or_dir" ]]; then
  has_explicit=0
  for a in "${pass_through[@]}"; do
    case "$a" in
      --reports-dir|--alert) has_explicit=1 ;;
    esac
  done
  [[ $has_explicit -eq 0 ]] && \
    date_or_dir="--reports-dir reports/$(date -u +%Y-%m-%d)"
fi

# shellcheck disable=SC2086
exec "$PY" "$SCRIPT_DIR/send_whatsapp_alerts.py" \
     --config "$ROOT/ingest/notifications.yaml" \
     $date_or_dir \
     "${pass_through[@]}"
