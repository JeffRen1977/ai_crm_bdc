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

## 3. Daily orchestrator

```bash
scripts/run-daily-pricredit.sh                                # all BDCs, default forms
scripts/run-daily-pricredit.sh --tickers ARCC,MAIN            # drill-down
FORMS=10-K,10-Q LIMIT_PER_FORM=2 scripts/run-daily-pricredit.sh
```

Behavior:

1. Refuses to run unless `PRICREDIT_UA_EMAIL` is set.
2. Refreshes `bdc/bdc_universe.json` if it's older than
   `UNIVERSE_MAX_AGE_H` (default 168h / 7 days) or missing.
3. Calls `fetch_filings.py` with the configured forms + limits.
4. Writes a log to `reports/<YYYY-MM-DD>/pricredit.log`.

## Environment / knobs

| Variable | Default | Purpose |
|---------|---------|---------|
| `PRICREDIT_UA_EMAIL` | (required) | Contact in EDGAR `User-Agent`. |
| `PYTHON` | `.venv/bin/python` → `python3` | Python interpreter. |
| `FORMS` | `10-K,10-Q,8-K` | Forms to download. |
| `LIMIT_PER_FORM` | `4` | Recent filings kept per form per BDC. |
| `PUBLIC_ONLY` | `1` | Skip BDCs that never became publicly traded. |
| `UNIVERSE_MAX_AGE_H` | `168` | Refresh universe if older than this many hours. |

## On-disk cache

HTTP responses (`company_tickers.json`, `submissions.json`,
filing `index.json`, primary docs) are memoized in `bdc/_cache/`
keyed by URL. Delete that directory to force a cold pull, or pass
`--force` to `fetch_filings.py` to re-download specific filings.

## What's next

`parse_filings.py`, `extract_portfolio.py`, `compute_risk.py`, and
`build_investor_report.py` land in follow-up commits. See
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for contracts and
[`AGENTS.md`](../AGENTS.md) for the full script roadmap.
