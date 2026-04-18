# PriCredit scripts

This folder holds the EDGAR client + ingestion CLIs. Everything runs
under `.venv` and talks to SEC EDGAR through the throttled session in
[`_edgar_common.py`](_edgar_common.py).

## Setup

```bash
cd PriCredit
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt

# EDGAR requires a real contact in the User-Agent. Set it once:
cat >> ~/.pricredit-env <<'EOF'
export PRICREDIT_UA_EMAIL=jianfengren.sd@gmail.com
EOF
source ~/.pricredit-env
```

If `PRICREDIT_UA_EMAIL` is missing or looks fake, all scripts refuse
to start — see [`docs/EDGAR_USAGE.md`](../docs/EDGAR_USAGE.md) for why.

## 1. Build the BDC universe

```bash
scripts/discover_bdcs.py --out bdc/bdc_universe.json
# With submissions.json hydration (slower — ~1 request per BDC):
scripts/discover_bdcs.py --hydrate
# Include BDCs that later withdrew (N-54C):
scripts/discover_bdcs.py --include-retired
```

Output schema:

```json
{
  "as_of": "2026-04-17",
  "source": "EDGAR EFTS (N-54A minus N-54C) + company_tickers.json",
  "counts": {"total": 183, "publicly_traded": 47, "retired_included": 0},
  "bdcs": [
    {
      "cik": "0001287750",
      "company_name": "Ares Capital Corp",
      "primary_ticker": "ARCC",
      "tickers": ["ARCC"],
      "publicly_traded": true,
      "bdc_elected": true,
      "bdc_retired": false,
      "evidence": {"n54a_count": 1, "n54c_count": 0}
    }
  ]
}
```

## 2. Fetch filings

```bash
scripts/fetch_filings.py                                      # defaults
scripts/fetch_filings.py --forms 10-K,10-Q --limit-per-form 4
scripts/fetch_filings.py --tickers ARCC,MAIN,OBDC
scripts/fetch_filings.py --ciks 0001287750,0001396440
scripts/fetch_filings.py --max-bdcs 5 --force                 # dev
scripts/fetch_filings.py --public-only                        # traded-only
```

Filings land at `filings/<cik>/<accession>/` with:

- `index.json` — EDGAR's filing index (what documents exist in this filing)
- `meta.json` — our summary (form, dates, URLs, whether primary doc saved)
- `<primary_document>` — the document itself (usually HTML or PDF)

## 3. Parse XBRL company facts

```bash
scripts/parse_filings.py --ticker ARCC --print-latest        # one BDC, verbose
scripts/parse_filings.py --tickers ARCC,MAIN,OBDC            # subset
scripts/parse_filings.py                                     # all publicly_traded
scripts/parse_filings.py --force                             # ignore 20h freshness
```

Outputs per BDC, under `extracted/<cik>/facts/`:

| File | Contents |
|------|----------|
| `timeseries.json` | Full history per canonical metric, dedup'd so 10-K/A restatements supersede originals. |
| `latest.json` | Most recent observation per metric. |
| `resolved.json` | Which XBRL tag each canonical metric resolved to (audit trail). |
| `summary.json` | One-page snapshot: latest values + derived ratios + NAV QoQ/YoY trend + disclaimer. |

Canonical metrics and the tag priority list live in
[`_xbrl_concepts.py`](_xbrl_concepts.py); see
[`docs/XBRL_CONCEPT_MAP.md`](../docs/XBRL_CONCEPT_MAP.md) for the
full catalog and extension procedure.

## 4. Score risk

```bash
scripts/compute_risk.py --tickers ARCC,MAIN,OBDC --print     # score + stderr audit
scripts/compute_risk.py --date 2026-04-18 --force             # pinned run date, overwrite
scripts/compute_risk.py --weights ingest/risk_weights.yaml    # alternate config
```

Consumes `extracted/<cik>/facts/summary.json` and writes:

| File | Contents |
|------|----------|
| `reports/<DATE>/risk_<ticker>.json` | Per-BDC scorecard: composite, band, per-factor raw/score/weight/contribution, curve used, fired alerts. |
| `reports/<DATE>/risk_summary.json` | Universe roll-up sorted by composite descending, with band counts. |
| `reports/<DATE>/alert_RISK-<ticker>-*.json` | One file per firing alert rule (idvault-compatible schema). |

The score model, curves, and alert rules are defined in
[`../ingest/risk_weights.yaml`](../ingest/risk_weights.yaml); the
methodology doc lives at
[`../docs/RISK_MODEL.md`](../docs/RISK_MODEL.md). Factors are
piecewise-linear; missing factors are excluded (not imputed) and
weights renormalize over the factors we had data for.

## 5. Email risk alerts

Alerts emitted by `compute_risk.py` land as
`reports/<DATE>/alert_RISK-<ticker>-*.json`. The dispatcher reads
those files, applies the severity / reason filter from
[`../ingest/notifications.yaml`](../ingest/notifications.yaml), and
emails the ones that pass via SMTP.

```bash
# dry-run today's alerts (no SMTP touched)
scripts/send-risk-alerts.sh --dry-run

# specific date
scripts/send-risk-alerts.sh 2026-04-18

# one summary email instead of one per alert
scripts/send-risk-alerts.sh 2026-04-18 --digest

# override recipient (test account)
scripts/send-risk-alerts.sh 2026-04-18 --to you@example.com --dry-run

# explicit file
scripts/send-risk-alerts.sh --alert reports/2026-04-18/alert_RISK-ARCC-20260418-001.json --dry-run
```

Credentials go in `~/.pricredit-env` (never commit):

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@example.com
export SMTP_PASSWORD=<app password>
export SMTP_FROM='PriCredit <you@example.com>'      # optional
# export SMTP_USE_SSL=1                             # if host uses SMTPS:465
# export SMTP_STARTTLS=0                            # if server negotiates plain
```

Behavior details:
- Severity ranking: `low < medium < high < critical`. Alerts below
  `email.min_severity_tier` are dropped.
- `email.include_reasons` (list) further restricts which rules get
  emailed; empty list = all.
- Idempotency: once SMTP accepts a message, a marker lands in
  `reports/<DATE>/.sent/<alert_id>.json` and reruns skip it
  (override with `--force`).
- `--digest` sends one email summarizing all alerts for the day,
  attaching `risk_summary.json`. Its own marker is
  `.sent/digest.json`.

## 6. Daily orchestrator

```bash
scripts/run-daily-pricredit.sh                                # ingest + parse + risk
scripts/run-daily-pricredit.sh --tickers ARCC,MAIN            # drill-down
scripts/run-daily-pricredit.sh --skip-parse                   # ingest only
scripts/run-daily-pricredit.sh --skip-risk                    # ingest + parse, no scoring
scripts/run-daily-pricredit.sh --send-alerts                  # + email alerts (per-alert)
scripts/run-daily-pricredit.sh --send-alerts --digest         # + one digest email
scripts/run-daily-pricredit.sh --send-alerts --alert-dry-run  # wire everything, don't send
FORMS=10-K,10-Q LIMIT_PER_FORM=2 scripts/run-daily-pricredit.sh
```

Behavior:

1. Refuses to run unless `PRICREDIT_UA_EMAIL` is set.
2. Refreshes `bdc/bdc_universe.json` if older than
   `UNIVERSE_MAX_AGE_H` (default 168h / 7 days) or missing.
3. Calls `fetch_filings.py` with the configured forms + limits.
4. Calls `parse_filings.py` (unless `--skip-parse` / `SKIP_PARSE=1`),
   writing a run summary to `reports/<DATE>/parse_summary.json`.
5. Calls `compute_risk.py` (unless `--skip-risk` / `SKIP_RISK=1` or
   parse was skipped), writing per-BDC scorecards, `risk_summary.json`,
   and one `alert_*.json` per firing rule into `reports/<DATE>/`.
6. **If `--send-alerts`**, invokes `send_risk_alerts.py` against the
   day's reports dir. `--digest` collapses to one email;
   `--alert-dry-run` composes without sending.
7. Log goes to `reports/<YYYY-MM-DD>/pricredit.log`.

## Environment / knobs

| Variable | Default | Purpose |
|---------|---------|---------|
| `PRICREDIT_UA_EMAIL` | (required) | Contact in EDGAR `User-Agent`. |
| `PYTHON` | `.venv/bin/python` → `python3` | Python interpreter. |
| `FORMS` | `10-K,10-Q,8-K` | Forms to download. |
| `LIMIT_PER_FORM` | `4` | Recent filings kept per form per BDC. |
| `PUBLIC_ONLY` | `1` | Skip BDCs that never became publicly traded. |
| `UNIVERSE_MAX_AGE_H` | `168` | Refresh universe if older than this many hours. |
| `SKIP_PARSE` | `0` | Set to `1` to skip the XBRL parse step (ingest only). |
| `SKIP_RISK` | `0` | Set to `1` to skip the risk scoring step. |
| `SEND_ALERTS` | `0` | Set to `1` to email alerts after scoring. |
| `SEND_DIGEST` | `0` | With `SEND_ALERTS=1`, send one digest instead of per-alert. |
| `ALERT_DRY_RUN` | `0` | Compose emails but don't touch SMTP. |
| `RISK_WEIGHTS` | `ingest/risk_weights.yaml` | Path to the risk engine config. |
| `SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` | — | SMTP credentials (put in `~/.pricredit-env`). |

## On-disk cache

HTTP responses (`company_tickers.json`, `submissions.json`,
filing `index.json`, primary docs) are memoized in `bdc/_cache/`
keyed by URL. Delete that directory to force a cold pull, or pass
`--force` to `fetch_filings.py` to re-download specific filings.

## What's next

`extract_portfolio.py` (Schedule of Investments — adds non-accrual %
and industry HHI as risk factors) and `build_investor_report.py`
land in follow-up commits. The investor report dispatcher will
reuse the SMTP / routing layer in `send_risk_alerts.py`. See
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for module
contracts and [`AGENTS.md`](../AGENTS.md) for the full script
roadmap.
