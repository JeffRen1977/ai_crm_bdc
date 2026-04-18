#!/usr/bin/env bash
# =============================================================================
# PriCredit — email today's alert_RISK-*.json files to the recipients in
# ingest/notifications.yaml.
#
# Usage:
#   scripts/send-risk-alerts.sh                       # today
#   scripts/send-risk-alerts.sh 2026-04-18            # specific date
#   scripts/send-risk-alerts.sh 2026-04-18 --dry-run
#   scripts/send-risk-alerts.sh 2026-04-18 --digest   # one summary email
#   scripts/send-risk-alerts.sh --to you@example.com reports/2026-04-18
#
# A trailing "reports/YYYY-MM-DD[/]" argument is treated as the reports dir.
# Otherwise the first positional matching YYYY-MM-DD is expanded to
# reports/<date>. All other args pass through to scripts/send_risk_alerts.py.
#
# Credentials (export in ~/.pricredit-env):
#   SMTP_HOST=smtp.gmail.com
#   SMTP_PORT=587
#   SMTP_USER=you@example.com
#   SMTP_PASSWORD=<app password>
#   SMTP_FROM='PriCredit <you@example.com>'   # optional, defaults to SMTP_USER
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

date_or_dir=""
pass_through=()
prev=""
for arg in "$@"; do
  # Args that take a value keep the next token attached to them.
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
exec "$PY" "$SCRIPT_DIR/send_risk_alerts.py" \
     --config "$ROOT/ingest/notifications.yaml" \
     $date_or_dir \
     "${pass_through[@]}"
