#!/usr/bin/env python3
"""
Discover the BDC universe from EDGAR.

A Business Development Company (BDC) is a closed-end fund that has
formally elected BDC status under section 54 of the Investment Company
Act of 1940. EDGAR records that election via **Form N-54A** and the
corresponding withdrawal via **Form N-54C**. Our universe is therefore
the set of filers that have filed at least one N-54A and have not
since filed an N-54C — plus a ticker-enrichment step so we know which
of them are publicly traded.

Strategy
--------
1.  Enumerate every filer that has ever submitted Form N-54A (BDC
    election) via EDGAR's **EFTS full-text search** at
    `efts.sec.gov/LATEST/search-index`. EFTS returns CIKs + display
    names paginated 100/page.
2.  Enumerate N-54C (withdrawal) filers the same way.
3.  Subtract (2) from (1). BDCs that later withdrew are dropped by
    default; keep them with `--include-retired` for auditing.
4.  Join against `www.sec.gov/files/company_tickers.json` to attach
    ticker(s). Filers with zero tickers stay in the universe but
    `publicly_traded=false`.
5.  (Optional, `--hydrate`) For each remaining CIK, pull
    `data.sec.gov/submissions/CIK<cik>.json` and copy in SIC code,
    exchanges, former names, state of incorporation, etc. Costs ~1
    request per CIK (~221 req on the current universe), skipped by
    default to keep the daily sweep fast.

Output
------
    bdc/bdc_universe.json
        { as_of, source, counts: {total, publicly_traded,
          retired_included}, bdcs: [ {cik, company_name,
          primary_ticker, tickers, publicly_traded, bdc_elected,
          bdc_retired, evidence, submissions?}, ... ] }

EDGAR compliance
----------------
All HTTP traffic goes through `_edgar_common.edgar_get_json`, which
enforces the contact-email `User-Agent`, ≤10 req/s throttling,
429/503 retry with jitter, and an on-disk response cache under
`bdc/_cache/`. `preflight()` hard-fails if the UA isn't configured.

Caching
-------
- EFTS search pages: 1h cache. Fresh enough to pick up a same-day
  new BDC election on the next daily run, cheap enough to avoid
  re-hitting the search API when reruns happen minutes apart.
- `company_tickers.json`: 24h cache. It updates roughly daily.
- `submissions.json` (when hydrating): 24h cache.

Examples
--------
    scripts/discover_bdcs.py --out bdc/bdc_universe.json    # default, no hydrate
    scripts/discover_bdcs.py --hydrate                      # + SIC/exchange enrichment
    scripts/discover_bdcs.py --include-retired              # keep withdrawn BDCs
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
    edgar_get_json,        # rate-limited + cached JSON GET
    company_tickers_url,   # https://www.sec.gov/files/company_tickers.json
    submissions_url,       # data.sec.gov/submissions/CIK<cik>.json (per filer)
    pad_cik,               # zero-pad CIK to 10 digits
    preflight,             # fail fast if UA email is not configured
)

# EFTS (Edgar Full-Text Search) is a separate backend from the
# documented data.sec.gov REST API. It exposes filings as an
# Elasticsearch-style index with {hits: {total, hits: [...]}} shape.
# We use it purely for *enumeration* of form filers (who has ever
# filed N-54A?), not for full-text search proper.
EFTS_SEARCH = "https://efts.sec.gov/LATEST/search-index"
ROOT = Path(__file__).resolve().parent.parent


def _efts_page(form: str, page_from: int, page_size: int = 100,
               date_from: str | None = None, date_to: str | None = None) -> dict:
    """Fetch one page of EFTS results for a given form.

    Page shape (abbreviated):

        {
          "hits": {
            "total": {"value": 221, ...},
            "hits": [
              {"_source": {
                  "ciks": ["0001287750"],
                  "display_names": ["ARES CAPITAL CORP  (CIK 0001287750)"],
                  "form": "N-54A", "file_date": "...", ...
              }}, ...
            ]
          }
        }

    `page_from` is the zero-based offset into the total hit list;
    pagination is simply offset+size. EFTS caps `from+size` at 10000
    but we never get close to that for N-54A/N-54C.
    """
    params = {
        "q": "",           # empty query = "all filings of this form"
        "forms": form,     # exact form match, e.g. "N-54A"
        "from": page_from,
        "size": page_size,
    }
    if date_from:
        params["dateRange"] = "custom"
        params["startdt"] = date_from
    if date_to:
        params["dateRange"] = "custom"
        params["enddt"] = date_to
    # Build the query string by hand: urlencode() percent-escapes
    # characters that EFTS accepts verbatim (commas in multi-form
    # searches, for instance) and occasionally returns empty results
    # when the exact bytes don't match. The values we pass here are
    # simple form names and integers, so manual join is safe.
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v not in ("", None))
    url = f"{EFTS_SEARCH}?{qs}"
    # 1-hour cache: short enough that a new N-54A filed today shows
    # up on tomorrow's run, long enough that local reruns within an
    # hour don't re-hit the EFTS backend.
    return edgar_get_json(url, cache_ttl_s=60 * 60)


def enumerate_form_filers(form: str, max_pages: int = 50) -> dict[str, dict]:
    """Walk EFTS pagination and return a dedup'd map of CIK -> filer info.

    Returns::

        {
          "0001287750": {
            "cik":           "0001287750",
            "company_name":  "ARES CAPITAL CORP",
            "filings_count": 3,      # number of N-54A filings by this CIK
          },
          ...
        }

    Notes:
    - A single CIK can have multiple filings of the same form (original
      + amendments). We dedupe by CIK and keep a count in
      `filings_count` so callers can audit the evidence — this is what
      later ends up as `evidence.n54a_count` / `n54c_count` in the
      universe file.
    - An EFTS hit can list multiple CIKs under `_source.ciks` when
      several filers co-sign the same submission. We credit each CIK
      independently.
    - `display_names[0]` looks like "ARES CAPITAL CORP  (CIK
      0001287750)". We strip the trailing " (CIK ...)" to get a clean
      company name.
    """
    out: dict[str, dict] = {}
    page_size = 100
    total = None
    for page in range(max_pages):
        js = _efts_page(form, page_from=page * page_size, page_size=page_size)
        hits = (js.get("hits") or {}).get("hits") or []
        if total is None:
            # EFTS reports the total hit count on every page; log it
            # once so operators can sanity-check that the form is
            # producing the expected rough count (~hundreds for N-54A).
            total = (js.get("hits") or {}).get("total", {}).get("value", 0)
            print(f"[discover] EFTS reports {total} hits for form={form}",
                  file=sys.stderr)
        if not hits:
            break
        for h in hits:
            src = h.get("_source") or {}
            ciks = src.get("ciks") or []
            name = (src.get("display_names") or [""])[0]
            # Strip the trailing " (CIK 0001234567)" that EFTS appends
            # to display names. `split(..., 1)` caps at one split so
            # companies that have "(CIK" in their actual name (rare)
            # aren't damaged further.
            company = name.split(" (CIK", 1)[0].strip() or name
            for cik in ciks:
                cik10 = pad_cik(cik)
                row = out.setdefault(cik10, {
                    "cik": cik10,
                    "company_name": company,
                    "filings_count": 0,
                })
                row["filings_count"] += 1
                # First non-empty company name wins. Later hits may
                # have a blank display_names on co-registrant filings;
                # don't overwrite a good name with an empty string.
                if not row.get("company_name") and company:
                    row["company_name"] = company
        # Stop as soon as we've walked the reported total; guards
        # against an infinite loop if EFTS keeps returning the same
        # page, which we've occasionally seen under high load.
        if total and (page + 1) * page_size >= total:
            break
        # Gentle pacing on top of the global 10 req/s throttle — keeps
        # us well under EDGAR's comfort threshold even when the cache
        # is cold and we're hitting EFTS hard.
        time.sleep(0.15)
    return out


def load_company_tickers() -> dict[str, list[dict]]:
    """Load EDGAR's public ticker map as {cik10: [{ticker, title}, ...]}.

    Source file shape (flat array, keyed by row index):

        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}

    A single CIK can appear multiple times when a company has several
    listed securities (e.g. common + preferred). We keep them all and
    let the caller pick `primary_ticker = tickers[0]` while retaining
    the full list on the universe entry.
    """
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
    """Return the subset of fields from `submissions.json` we carry into
    the universe entry when `--hydrate` is set.

    Best-effort: a single-CIK transport error (EDGAR 503, cached miss,
    etc.) returns {} rather than aborting the whole discovery run.
    The caller still gets a valid entry, just without the enrichment
    sub-block.

    Fields picked:
        sic / sic_description : SEC industry code (6726 = "Investment
                                Offices NEC" for most BDCs).
        category              : e.g. "Large accelerated filer".
        exchanges             : ["NASDAQ"], ["NYSE"], etc.
        tickers               : authoritative ticker list from
                                submissions.json (can differ from
                                company_tickers.json in edge cases).
        former_names          : {name, from, to} history — useful for
                                matching historical filings when a
                                BDC has been renamed.
        ein, state_of_incorporation, entity_type : identity metadata.
    """
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
        # `formerNames` in submissions.json is a list of
        # {name, from, to}; we keep only the name strings for the
        # universe entry. The full objects stay a hydrate away via
        # the cache if a caller ever needs them.
        "former_names": [fn.get("name") for fn in js.get("formerNames") or []],
        "ein": js.get("ein"),
        "state_of_incorporation": js.get("stateOfIncorporation"),
        "entity_type": js.get("entityType"),
    }


def build_universe(
    hydrate: bool,
    include_retired: bool,
) -> dict[str, Any]:
    """Run the full discovery pipeline and return the universe dict.

    Algorithm (set-theoretic):

        U = { cik : filed N-54A } \\ { cik : filed N-54C }    (default)
        U = { cik : filed N-54A }                            (include_retired=True)

    Each CIK in U is then joined with company_tickers.json and
    (optionally) submissions.json for identity enrichment.
    """
    print("[discover] fetching N-54A filers (BDC election)...", file=sys.stderr)
    elected = enumerate_form_filers("N-54A")
    print(f"[discover]   elected: {len(elected)} CIKs", file=sys.stderr)

    print("[discover] fetching N-54C filers (BDC withdrawal)...", file=sys.stderr)
    retired = enumerate_form_filers("N-54C")
    print(f"[discover]   retired: {len(retired)} CIKs", file=sys.stderr)

    # Precomputed for readability; the actual membership test below
    # uses `cik in retired` so we also have the filings_count handy.
    retired_ciks = set(retired.keys()) if not include_retired else set()

    print("[discover] loading company_tickers.json...", file=sys.stderr)
    tickers = load_company_tickers()

    bdcs: list[dict[str, Any]] = []
    # Sorted iteration gives deterministic output so the committed
    # universe file diffs stably when only a single BDC is added.
    for cik, row in sorted(elected.items()):
        is_retired = cik in retired
        if is_retired and not include_retired:
            continue
        ticker_rows = tickers.get(cik, [])
        # Convention: `primary_ticker` is the first entry in the
        # company_tickers.json row order for this CIK. For a BDC with
        # a single common-stock ticker (the typical case) this is
        # unambiguous; for a BDC with preferred securities listed
        # separately, the first listing wins.
        primary_ticker = ticker_rows[0]["ticker"] if ticker_rows else None
        entry = {
            "cik": cik,
            "company_name": row["company_name"],
            "primary_ticker": primary_ticker,
            # `tickers` is explicitly None (not []) when the BDC is
            # not publicly traded, so downstream readers can easily
            # distinguish "no ticker data" from "checked and empty".
            "tickers": [t["ticker"] for t in ticker_rows] or None,
            "publicly_traded": bool(ticker_rows),
            "bdc_elected": True,
            "bdc_retired": is_retired,
            # Audit trail: how many N-54A / N-54C filings this CIK
            # has. A `bdc_retired=false` entry with `n54c_count > 0`
            # would indicate `--include-retired` was used on a
            # historical run — useful for debugging universe diffs.
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
    ap.add_argument("--out", default=str(ROOT / "bdc" / "bdc_universe.json"),
                    help="Output path for the universe JSON "
                         "(default: bdc/bdc_universe.json).")
    ap.add_argument("--hydrate", action="store_true",
                    help="Enrich each CIK with submissions.json fields "
                         "(SIC code, exchanges, former names, state of "
                         "incorporation, etc.). Costs ~1 EDGAR request "
                         "per CIK, so budget a few seconds of wall time.")
    ap.add_argument("--no-hydrate", dest="hydrate", action="store_false",
                    help="Explicitly disable hydration (the default).")
    ap.set_defaults(hydrate=False)
    ap.add_argument("--include-retired", action="store_true",
                    help="Keep filers that later submitted N-54C. "
                         "Useful for historical analysis; excluded by "
                         "default since the daily pipeline only scores "
                         "active BDCs.")
    args = ap.parse_args()

    # Fail fast if the EDGAR UA isn't configured. Without this we'd
    # fire dozens of EFTS requests that all come back as 403.
    preflight()

    universe = build_universe(hydrate=args.hydrate,
                              include_retired=args.include_retired)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Pretty-printed, stable key order, UTF-8. The universe file is
    # small (tens of KB even at 200+ BDCs) so optimizing for
    # human-readable diffs is the right call.
    out_path.write_text(json.dumps(universe, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    c = universe["counts"]
    print(f"[discover] wrote {out_path} | total={c['total']}"
          f" public={c['publicly_traded']} retired_kept={c['retired_included']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
