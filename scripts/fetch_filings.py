#!/usr/bin/env python3
"""
Download recent EDGAR filings for BDCs in bdc/bdc_universe.json.

For each CIK we read `data.sec.gov/submissions/CIK{cik}.json`, pick the
most recent filings of the requested forms (default 10-K, 10-Q, 8-K),
and pull both the filing's index.json (tiny) and its primary document
(HTML/PDF/XML) into `filings/<cik>/<accession>/`.

Outputs:
    filings/<cik>/<accession>/index.json      EDGAR-provided filing index
    filings/<cik>/<accession>/meta.json       our metadata (form, dates, urls)
    filings/<cik>/<accession>/<primary_doc>   the primary document bytes

Example:
    scripts/fetch_filings.py                            # default universe + forms
    scripts/fetch_filings.py --forms 10-K,10-Q
    scripts/fetch_filings.py --tickers ARCC,MAIN,OBDC --limit-per-form 2
    scripts/fetch_filings.py --max-bdcs 10              # dev throttle
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
    archive_url,
    edgar_get_bytes,
    edgar_get_json,
    pad_cik,
    preflight,
    submissions_url,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"
DEFAULT_OUT = ROOT / "filings"
DEFAULT_FORMS = ["10-K", "10-Q", "8-K"]


def _iter_recent(submissions: dict) -> list[dict]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    keys = ["accessionNumber", "form", "filingDate", "reportDate",
            "primaryDocument", "primaryDocDescription", "fileNumber",
            "isInlineXBRL", "size"]
    arrays = {k: recent.get(k) or [] for k in keys}
    n = len(arrays["accessionNumber"])
    out = []
    for i in range(n):
        out.append({k: arrays[k][i] if i < len(arrays[k]) else None for k in keys})
    return out


def pick_filings(submissions: dict, forms: list[str], limit_per_form: int) -> list[dict]:
    picks: dict[str, list[dict]] = {f: [] for f in forms}
    for row in _iter_recent(submissions):
        form = row.get("form") or ""
        if form in picks and len(picks[form]) < limit_per_form:
            picks[form].append(row)
    flat: list[dict] = []
    for form in forms:
        flat.extend(picks[form])
    return flat


def fetch_one(cik: str, filing: dict, out_root: Path, force: bool) -> dict:
    accession = filing["accessionNumber"]
    if not accession:
        return {"skipped": True, "reason": "missing accession"}
    acc_clean = accession.replace("-", "")
    target_dir = out_root / pad_cik(cik) / acc_clean
    target_dir.mkdir(parents=True, exist_ok=True)

    meta_path = target_dir / "meta.json"
    if meta_path.exists() and not force:
        return {"skipped": True, "reason": "already fetched",
                "path": str(target_dir)}

    idx_url = archive_url(cik, accession, "index.json")
    try:
        idx = edgar_get_json(idx_url, cache_ttl_s=7 * 24 * 60 * 60)
    except Exception as exc:
        return {"skipped": True, "reason": f"index.json failed: {exc}"}

    (target_dir / "index.json").write_text(
        json.dumps(idx, indent=2), encoding="utf-8"
    )

    primary = filing.get("primaryDocument") or ""
    primary_ok = False
    primary_path: Path | None = None
    if primary:
        pdoc_url = archive_url(cik, accession, primary)
        try:
            blob = edgar_get_bytes(pdoc_url, cache_ttl_s=7 * 24 * 60 * 60)
            primary_path = target_dir / primary
            primary_path.parent.mkdir(parents=True, exist_ok=True)
            primary_path.write_bytes(blob)
            primary_ok = True
        except Exception as exc:
            print(f"[fetch] {cik} {accession} primary doc failed: {exc}",
                  file=sys.stderr)

    meta = {
        "cik": pad_cik(cik),
        "accession_number": accession,
        "form": filing.get("form"),
        "filing_date": filing.get("filingDate"),
        "report_date": filing.get("reportDate"),
        "primary_document": primary,
        "primary_doc_description": filing.get("primaryDocDescription"),
        "file_number": filing.get("fileNumber"),
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
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--forms", default=",".join(DEFAULT_FORMS),
                    help="Comma-separated form list (default: 10-K,10-Q,8-K)")
    ap.add_argument("--limit-per-form", type=int, default=4,
                    help="How many recent filings per form per BDC (default 4)")
    ap.add_argument("--tickers", default="",
                    help="Comma-separated tickers to restrict to (case-insensitive)")
    ap.add_argument("--ciks", default="",
                    help="Comma-separated CIKs to restrict to")
    ap.add_argument("--max-bdcs", type=int, default=0,
                    help="Cap BDCs processed this run (0 = all); dev-only.")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if meta.json already exists.")
    ap.add_argument("--public-only", action="store_true",
                    help="Skip BDCs flagged publicly_traded=false.")
    args = ap.parse_args()

    preflight()

    univ_path = Path(args.universe)
    if not univ_path.exists():
        print(f"[fetch] universe not found: {univ_path}\n"
              f"        Run scripts/discover_bdcs.py first.",
              file=sys.stderr)
        return 2

    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    universe = json.loads(univ_path.read_text(encoding="utf-8"))
    bdcs: list[dict] = universe.get("bdcs") or []

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
        tag = b.get("primary_ticker") or b.get("company_name") or cik
        try:
            subs = edgar_get_json(submissions_url(cik), cache_ttl_s=24 * 60 * 60)
        except Exception as exc:
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
