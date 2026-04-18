#!/usr/bin/env python3
"""
Discover publicly traded BDCs from EDGAR.

Strategy:
  1. Enumerate every filer that has ever submitted a Form N-54A (the
     election to be regulated as a BDC). EDGAR's EFTS full-text search
     returns CIKs + company names.
  2. Subtract any filer that later submitted N-54C (withdrawal).
  3. For each remaining CIK, hydrate with ticker(s) from
     company_tickers.json. A BDC that isn't publicly traded has no
     ticker — we keep it anyway but mark `publicly_traded: false`.
  4. Optionally further enrich with sic_code + category from
     submissions.json (costs 1 request/CIK — skip with `--no-hydrate`).

Output: bdc/bdc_universe.json

Example:
    scripts/discover_bdcs.py --out bdc/bdc_universe.json
    scripts/discover_bdcs.py --min-year 2000 --no-hydrate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edgar_common import (  # noqa: E402
    edgar_get_json,
    company_tickers_url,
    submissions_url,
    pad_cik,
    preflight,
)

EFTS_SEARCH = "https://efts.sec.gov/LATEST/search-index"
ROOT = Path(__file__).resolve().parent.parent


def _efts_page(form: str, page_from: int, page_size: int = 100,
               date_from: str | None = None, date_to: str | None = None) -> dict:
    params = {
        "q": "",
        "forms": form,
        "from": page_from,
        "size": page_size,
    }
    if date_from:
        params["dateRange"] = "custom"
        params["startdt"] = date_from
    if date_to:
        params["dateRange"] = "custom"
        params["enddt"] = date_to
    # EFTS expects query string; we build manually to keep characters stable.
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v not in ("", None))
    url = f"{EFTS_SEARCH}?{qs}"
    return edgar_get_json(url, cache_ttl_s=60 * 60)  # 1h cache


def enumerate_form_filers(form: str, max_pages: int = 50) -> dict[str, dict]:
    """Return {cik10: {"company": str, "ciks": [cik10], "filings": int}}."""
    out: dict[str, dict] = {}
    page_size = 100
    total = None
    for page in range(max_pages):
        js = _efts_page(form, page_from=page * page_size, page_size=page_size)
        hits = (js.get("hits") or {}).get("hits") or []
        if total is None:
            total = (js.get("hits") or {}).get("total", {}).get("value", 0)
            print(f"[discover] EFTS reports {total} hits for form={form}",
                  file=sys.stderr)
        if not hits:
            break
        for h in hits:
            src = h.get("_source") or {}
            ciks = src.get("ciks") or []
            name = (src.get("display_names") or [""])[0]
            company = name.split(" (CIK", 1)[0].strip() or name
            for cik in ciks:
                cik10 = pad_cik(cik)
                row = out.setdefault(cik10, {
                    "cik": cik10,
                    "company_name": company,
                    "filings_count": 0,
                })
                row["filings_count"] += 1
                if not row.get("company_name") and company:
                    row["company_name"] = company
        if total and (page + 1) * page_size >= total:
            break
        time.sleep(0.15)
    return out


def load_company_tickers() -> dict[str, list[dict]]:
    """Return {cik10: [{ticker, title}, ...]}."""
    raw = edgar_get_json(company_tickers_url(), cache_ttl_s=24 * 60 * 60)
    out: dict[str, list[dict]] = {}
    for _key, row in raw.items():
        cik10 = pad_cik(row.get("cik_str"))
        out.setdefault(cik10, []).append({
            "ticker": row.get("ticker"),
            "title": row.get("title"),
        })
    return out


def hydrate_from_submissions(cik: str) -> dict:
    try:
        js = edgar_get_json(submissions_url(cik), cache_ttl_s=24 * 60 * 60)
    except Exception as exc:  # pragma: no cover
        print(f"[discover] hydrate {cik}: {exc}", file=sys.stderr)
        return {}
    return {
        "sic": js.get("sic"),
        "sic_description": js.get("sicDescription"),
        "category": js.get("category"),
        "exchanges": js.get("exchanges") or [],
        "tickers": js.get("tickers") or [],
        "former_names": [fn.get("name") for fn in js.get("formerNames") or []],
        "ein": js.get("ein"),
        "state_of_incorporation": js.get("stateOfIncorporation"),
        "entity_type": js.get("entityType"),
    }


def build_universe(
    hydrate: bool,
    include_retired: bool,
) -> dict[str, Any]:
    print("[discover] fetching N-54A filers (BDC election)...", file=sys.stderr)
    elected = enumerate_form_filers("N-54A")
    print(f"[discover]   elected: {len(elected)} CIKs", file=sys.stderr)

    print("[discover] fetching N-54C filers (BDC withdrawal)...", file=sys.stderr)
    retired = enumerate_form_filers("N-54C")
    print(f"[discover]   retired: {len(retired)} CIKs", file=sys.stderr)

    retired_ciks = set(retired.keys()) if not include_retired else set()

    print("[discover] loading company_tickers.json...", file=sys.stderr)
    tickers = load_company_tickers()

    bdcs: list[dict[str, Any]] = []
    for cik, row in sorted(elected.items()):
        is_retired = cik in retired
        if is_retired and not include_retired:
            continue
        ticker_rows = tickers.get(cik, [])
        primary_ticker = ticker_rows[0]["ticker"] if ticker_rows else None
        entry = {
            "cik": cik,
            "company_name": row["company_name"],
            "primary_ticker": primary_ticker,
            "tickers": [t["ticker"] for t in ticker_rows] or None,
            "publicly_traded": bool(ticker_rows),
            "bdc_elected": True,
            "bdc_retired": is_retired,
            "evidence": {
                "n54a_count": row["filings_count"],
                "n54c_count": retired.get(cik, {}).get("filings_count", 0),
            },
        }
        if hydrate:
            entry["submissions"] = hydrate_from_submissions(cik)
        bdcs.append(entry)

    return {
        "as_of": time.strftime("%Y-%m-%d", time.gmtime()),
        "source": "EDGAR EFTS (N-54A minus N-54C) + company_tickers.json",
        "counts": {
            "total": len(bdcs),
            "publicly_traded": sum(1 for b in bdcs if b["publicly_traded"]),
            "retired_included": sum(1 for b in bdcs if b["bdc_retired"]),
        },
        "bdcs": bdcs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "bdc" / "bdc_universe.json"))
    ap.add_argument("--hydrate", action="store_true",
                    help="Enrich each CIK with submissions.json (1 req/CIK).")
    ap.add_argument("--no-hydrate", dest="hydrate", action="store_false")
    ap.set_defaults(hydrate=False)
    ap.add_argument("--include-retired", action="store_true",
                    help="Keep filers that later submitted N-54C.")
    args = ap.parse_args()

    preflight()
    universe = build_universe(hydrate=args.hydrate,
                              include_retired=args.include_retired)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(universe, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    c = universe["counts"]
    print(f"[discover] wrote {out_path} | total={c['total']}"
          f" public={c['publicly_traded']} retired_kept={c['retired_included']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
