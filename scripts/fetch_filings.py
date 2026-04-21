#!/usr/bin/env python3
"""
Download recent EDGAR filings for every BDC in bdc/bdc_universe.json.

Strategy
--------
For each CIK in the universe we:

1.  Pull `data.sec.gov/submissions/CIK{cik}.json`. This file holds the
    company's entire filing history as parallel arrays under
    `filings.recent.*` (accession numbers, form types, dates, primary
    doc names, etc.). EDGAR indexes this file heavily, so it's the
    cheapest way to enumerate a filer's submissions.
2.  Walk those parallel arrays, bucket by `form`, and keep the most
    recent N per form (default 4). Order inside `recent.*` is
    EDGAR-native, which is already newest-first.
3.  For each selected filing, download two artifacts into
    `filings/<cik>/<accession>/`:
      - `index.json` (tiny) — the filing's manifest, listing every
        exhibit/document in the submission.
      - `<primary_document>` (HTML, iXBRL HTML, PDF, or XML) — the
        actual filed document, which for 10-K/10-Q BDC filings
        carries the inline XBRL facts we mine downstream.
    A third file, `meta.json`, is written by this script as our own
    bookkeeping: provenance + a local path pointer so later stages
    (parse_filings.py, extract_portfolio.py) don't have to re-derive
    URLs.

Idempotency
-----------
`fetch_one` skips any accession where `meta.json` already exists
unless `--force` is passed, so reruns are cheap (O(1 submissions
request per CIK). The on-disk HTTP cache in `_edgar_common.py`
absorbs the submissions.json round-trip on top of that.

EDGAR compliance
----------------
All HTTP traffic flows through `_edgar_common.edgar_get_*`, which
enforces:
  - a contact-email `User-Agent` (preflight raises if missing),
  - ≤10 req/s rate limiting,
  - retries with jitter on 429/503,
  - a keyed response cache under `bdc/_cache/`.

Outputs (per accession)
-----------------------
    filings/<cik>/<accession>/index.json    EDGAR's filing index
    filings/<cik>/<accession>/meta.json     our metadata (form, dates, urls, ...)
    filings/<cik>/<accession>/<primary_doc> the primary document bytes

Examples
--------
    scripts/fetch_filings.py                                  # whole universe + default forms
    scripts/fetch_filings.py --forms 10-K,10-Q
    scripts/fetch_filings.py --tickers ARCC,MAIN,OBDC --limit-per-form 2
    scripts/fetch_filings.py --ciks 0001287750,0001396440
    scripts/fetch_filings.py --max-bdcs 10                    # dev throttle
    scripts/fetch_filings.py --public-only                    # traded BDCs only
    scripts/fetch_filings.py --force                          # re-download everything
    scripts/fetch_filings.py --include-registration --tickers ARCC --limit-per-form 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edgar_common import (  # noqa: E402
    archive_url,       # www.sec.gov/Archives URL builder (un-padded CIK)
    edgar_get_bytes,   # rate-limited + cached binary GET
    edgar_get_json,    # same, decoded as JSON
    pad_cik,           # zero-pad CIK to 10 digits (required by data.sec.gov)
    preflight,         # fail fast if UA email is not configured
    submissions_url,   # build data.sec.gov/submissions/CIK<cik>.json URL
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"
DEFAULT_OUT = ROOT / "filings"

# 10-K = annual report, 10-Q = quarterly report, 8-K = material event.
# These three cover the signals our risk engine + portfolio extractor
# consume today. Extend via --forms if additional forms (e.g. DEF 14A,
# N-CSR) become relevant.
DEFAULT_FORMS = ["10-K", "10-Q", "8-K"]

# BDC registration / prospectus family — fee schedules, investment limits,
# risk factors (see docs/BDC_PRIMER.md). Not part of DEFAULT_FORMS so the
# daily universe sweep stays bounded; use --include-registration (or
# INCLUDE_REGISTRATION=1 in run-daily-pricredit.sh) when building the
# Investor Reporting / compliance layer. EDGAR matches form strings
# exactly (N-2/A is distinct from N-2).
REGISTRATION_FORMS = [
    "N-2",
    "N-2/A",
    "424B2",
    "424B3",
    "424B5",
    "497",
]


def _iter_recent(submissions: dict) -> list[dict]:
    """Flatten `submissions.filings.recent` from parallel arrays to a
    list of row dicts.

    EDGAR stores the recent filing history as column-major arrays:

        {
          "filings": {
            "recent": {
              "accessionNumber": ["0001287750-26-000006", ...],
              "form":            ["10-K",                 ...],
              "filingDate":      ["2026-02-04",           ...],
              ...
            },
            "files": [ ... older batches, paginated ... ]
          }
        }

    We only touch `recent.*` here; historical batches in `files[]`
    require an extra hop and we don't need deep history for the
    daily pipeline (universe refresh of recent filings is enough).

    Order inside `recent.*` is newest-first, so the caller can pick
    top-N per form without sorting.
    """
    recent = (submissions.get("filings") or {}).get("recent") or {}
    keys = ["accessionNumber", "form", "filingDate", "reportDate",
            "primaryDocument", "primaryDocDescription", "fileNumber",
            "isInlineXBRL", "size"]
    arrays = {k: recent.get(k) or [] for k in keys}
    # `accessionNumber` is the authoritative spine — all other arrays
    # should be parallel-indexed to it. Use its length as n; tolerate
    # shorter sibling arrays with None fallback.
    n = len(arrays["accessionNumber"])
    out = []
    for i in range(n):
        out.append({k: arrays[k][i] if i < len(arrays[k]) else None for k in keys})
    return out


def pick_filings(submissions: dict, forms: list[str], limit_per_form: int) -> list[dict]:
    """Select the most recent `limit_per_form` filings for each form in
    `forms`.

    Returns a flat list grouped by form in the caller's order (so
    `forms=["10-K", "10-Q", "8-K"]` yields 10-Ks first, then 10-Qs,
    then 8-Ks). This grouping is purely cosmetic for the run log —
    the downstream consumers don't rely on it.
    """
    picks: dict[str, list[dict]] = {f: [] for f in forms}
    for row in _iter_recent(submissions):
        form = row.get("form") or ""
        # Form types are exact-match strings ("10-K/A" ≠ "10-K"); we
        # deliberately don't collapse amendments into their base form
        # here so the caller can opt in via --forms if they want them.
        if form in picks and len(picks[form]) < limit_per_form:
            picks[form].append(row)
    flat: list[dict] = []
    for form in forms:
        flat.extend(picks[form])
    return flat


def fetch_one(cik: str, filing: dict, out_root: Path, force: bool) -> dict:
    """Download one filing's index + primary document and write our
    meta.json sidecar.

    Idempotent: if `meta.json` already exists for the accession and
    `force=False`, returns early without touching EDGAR. Delete the
    file (or pass `--force`) to force a re-pull.

    Returns a small status dict; never raises — transport errors are
    surfaced via `{"skipped": True, "reason": "..."}` so a partial
    universe still completes.
    """
    accession = filing["accessionNumber"]
    if not accession:
        return {"skipped": True, "reason": "missing accession"}

    # EDGAR uses *two* accession formats interchangeably:
    #   "0001287750-26-000006"  — the human-readable form used in filings.recent
    #   "000128775026000006"    — the no-dash form used as the archive path
    # We keep the human form in meta.json and the no-dash form on disk
    # to match `www.sec.gov/Archives/edgar/data/<cik>/<acc_clean>/`.
    acc_clean = accession.replace("-", "")
    target_dir = out_root / pad_cik(cik) / acc_clean
    target_dir.mkdir(parents=True, exist_ok=True)

    meta_path = target_dir / "meta.json"
    if meta_path.exists() and not force:
        return {"skipped": True, "reason": "already fetched",
                "path": str(target_dir)}

    # The filing index lists every file in the submission (main doc,
    # exhibits, R*.htm rendered tables, Financial_Report.xlsx, etc.).
    # We cache it for 7 days: filings are immutable once accepted, so
    # only amendments cause churn and those get their own accession.
    idx_url = archive_url(cik, accession, "index.json")
    try:
        idx = edgar_get_json(idx_url, cache_ttl_s=7 * 24 * 60 * 60)
    except Exception as exc:
        return {"skipped": True, "reason": f"index.json failed: {exc}"}

    (target_dir / "index.json").write_text(
        json.dumps(idx, indent=2), encoding="utf-8"
    )

    # Primary document fetch is best-effort: some very old filings
    # (pre-2001, mostly) have null primaryDocument, and a handful of
    # 8-K/A amendments point at a file that's missing from archives.
    # We still want to keep the index + meta even if this fails, so
    # downstream steps (like portfolio extraction) can tell the
    # document is unavailable without re-hitting EDGAR.
    primary = filing.get("primaryDocument") or ""
    primary_ok = False
    primary_path: Path | None = None
    if primary:
        pdoc_url = archive_url(cik, accession, primary)
        try:
            blob = edgar_get_bytes(pdoc_url, cache_ttl_s=7 * 24 * 60 * 60)
            # `primary` can contain subdirectories (e.g., "exhibits/foo.htm"),
            # so ensure the parent exists before writing.
            primary_path = target_dir / primary
            primary_path.parent.mkdir(parents=True, exist_ok=True)
            primary_path.write_bytes(blob)
            primary_ok = True
        except Exception as exc:
            print(f"[fetch] {cik} {accession} primary doc failed: {exc}",
                  file=sys.stderr)

    # meta.json is the contract between this script and every
    # downstream consumer (parse_filings.py, extract_portfolio.py,
    # build_investor_report.py). Keep field names stable; add new
    # fields additively.
    meta = {
        "cik": pad_cik(cik),
        "accession_number": accession,
        "form": filing.get("form"),
        "filing_date": filing.get("filingDate"),
        "report_date": filing.get("reportDate"),
        "primary_document": primary,
        "primary_doc_description": filing.get("primaryDocDescription"),
        "file_number": filing.get("fileNumber"),
        # isInlineXBRL is 0/1 in the EDGAR payload. Downstream readers
        # should treat any truthy value as "iXBRL available".
        "is_inline_xbrl": filing.get("isInlineXBRL"),
        "size": filing.get("size"),
        "archive_url": archive_url(cik, accession),
        "index_url": idx_url,
        "primary_url": archive_url(cik, accession, primary) if primary else None,
        "primary_saved": primary_ok,
        "local_primary_path": str(primary_path) if primary_path else None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    return {"skipped": False, "path": str(target_dir), "meta": meta}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE),
                    help="Path to bdc_universe.json (default: bdc/bdc_universe.json).")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="Output root for downloaded filings (default: filings/).")
    ap.add_argument("--forms", default=",".join(DEFAULT_FORMS),
                    help="Comma-separated form list (default: 10-K,10-Q,8-K). "
                         "Exact EDGAR form strings; amendments like '10-K/A' "
                         "must be listed explicitly.")
    ap.add_argument("--include-registration", action="store_true",
                    help="Append registration/prospectus forms (%s) for fee "
                         "limits, N-2 disclosures, and 424B prospectuses. "
                         "Off by default — adds several filings per BDC at "
                         "the current --limit-per-form."
                         % ",".join(REGISTRATION_FORMS))
    ap.add_argument("--limit-per-form", type=int, default=4,
                    help="How many most-recent filings per form per BDC "
                         "(default 4 — typically 1 fiscal year of 10-K + "
                         "the last ~4 quarters of 10-Q).")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated tickers to restrict to "
                         "(case-insensitive). Matches against both "
                         "primary_ticker and the full tickers[] array.")
    ap.add_argument("--ciks", default="",
                    help="Comma-separated CIKs to restrict to. "
                         "CIKs are zero-padded internally — accepts "
                         "either '1287750' or '0001287750'.")
    ap.add_argument("--max-bdcs", type=int, default=0,
                    help="Cap BDCs processed this run (0 = all). Dev-only; "
                         "applied after --tickers/--ciks/--public-only.")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if meta.json already exists. "
                         "Only affects existing accessions; does not widen "
                         "the per-form selection.")
    ap.add_argument("--public-only", action="store_true",
                    help="Skip BDCs flagged publicly_traded=false in the "
                         "universe. Non-traded BDCs still file with EDGAR "
                         "but usually aren't in scope for investor reporting.")
    args = ap.parse_args()

    # Fail fast if the EDGAR User-Agent isn't set up. This avoids
    # firing off dozens of requests that would all come back as 403.
    preflight()

    univ_path = Path(args.universe)
    if not univ_path.exists():
        print(f"[fetch] universe not found: {univ_path}\n"
              f"        Run scripts/discover_bdcs.py first.",
              file=sys.stderr)
        return 2

    # Normalize forms to uppercase so "10-k" / "10-K" are equivalent
    # on the CLI; EDGAR itself is case-sensitive and only returns
    # uppercase, so we compare in upper form.
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    if args.include_registration:
        seen = set(forms)
        for rf in REGISTRATION_FORMS:
            if rf not in seen:
                forms.append(rf)
                seen.add(rf)
    universe = json.loads(univ_path.read_text(encoding="utf-8"))
    bdcs: list[dict] = universe.get("bdcs") or []

    # Filter chain. Order is intentional:
    #   public-only -> tickers -> ciks -> max-bdcs
    # so --max-bdcs is applied last and doesn't accidentally chop off
    # a requested ticker.
    if args.public_only:
        bdcs = [b for b in bdcs if b.get("publicly_traded")]

    if args.tickers.strip():
        wanted = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        bdcs = [b for b in bdcs if
                (b.get("primary_ticker") or "").upper() in wanted
                or any((t or "").upper() in wanted for t in (b.get("tickers") or []))]
    if args.ciks.strip():
        wanted = {pad_cik(c.strip()) for c in args.ciks.split(",") if c.strip()}
        bdcs = [b for b in bdcs if b.get("cik") in wanted]
    if args.max_bdcs:
        bdcs = bdcs[:args.max_bdcs]

    if not bdcs:
        print("[fetch] no BDCs match filters; nothing to do.", file=sys.stderr)
        return 0

    print(f"[fetch] forms={forms} limit_per_form={args.limit_per_form}"
          f" bdcs={len(bdcs)}", file=sys.stderr)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    total_attempted = 0
    total_fetched = 0
    total_skipped = 0
    for b in bdcs:
        cik = b["cik"]
        # Human-friendly tag for log lines — prefer ticker, fall back
        # to company name, then raw CIK.
        tag = b.get("primary_ticker") or b.get("company_name") or cik

        # submissions.json changes daily (new filings are appended),
        # so we cache it for 24h — short enough to pick up today's
        # filings on the next run, long enough to keep the daily
        # universe sweep under a few seconds.
        try:
            subs = edgar_get_json(submissions_url(cik), cache_ttl_s=24 * 60 * 60)
        except Exception as exc:
            # Don't abort the run — a single BDC's submissions can be
            # transiently unavailable (EDGAR 503) without affecting
            # the other 51. The risk engine will see stale data for
            # this one and move on.
            print(f"[fetch] {tag} submissions failed: {exc}", file=sys.stderr)
            continue

        picks = pick_filings(subs, forms, args.limit_per_form)
        for f in picks:
            total_attempted += 1
            res = fetch_one(cik, f, out_root, force=args.force)
            if res.get("skipped"):
                total_skipped += 1
                reason = res.get("reason", "")
                print(f"[fetch]   {tag} {f.get('form')} {f.get('accessionNumber')}:"
                      f" skip ({reason})", file=sys.stderr)
            else:
                total_fetched += 1
                print(f"[fetch]   {tag} {f.get('form')} {f.get('accessionNumber')}"
                      f" -> {res['path']}", file=sys.stderr)

    print(f"[fetch] done: attempted={total_attempted} fetched={total_fetched}"
          f" skipped={total_skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
