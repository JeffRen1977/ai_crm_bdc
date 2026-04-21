#!/usr/bin/env python3
"""
Minimal 8-K item scraper (ARCC-first) from locally cached EDGAR filings.

For each selected filer we:
  1) scan local `filings/<cik>/<accession>/meta.json`,
  2) keep 8-K / 8-K/A filings with locally saved primary docs,
  3) extract Item references + basic event flags,
  4) write `events_8k.json` per filing under `extracted/<cik>/events8k/`.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILINGS = ROOT / "filings"
DEFAULT_OUT = ROOT / "extracted"
DEFAULT_UNIVERSE = ROOT / "bdc" / "bdc_universe.json"

DEFAULT_CIK = "0001287750"
DEFAULT_TICKER = "ARCC"
SUPPORTED_FORMS = {"8-K", "8-K/A"}

ITEM_RE = re.compile(r"\bitem\s+([0-9]{1,2}\.[0-9]{2})\b", re.IGNORECASE)
WS_RE = re.compile(r"\s+")
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
COMMENT_RE = re.compile(r"(?is)<!--.*?-->")
TAG_RE = re.compile(r"(?is)<[^>]+>")

KEYWORDS = {
    "credit_facility_change": [
        re.compile(r"\bcredit\s+facility\b", re.IGNORECASE),
        re.compile(r"\brevolving\s+credit\b", re.IGNORECASE),
        re.compile(r"\bterm\s+loan\b", re.IGNORECASE),
        re.compile(r"\b(amended|amendment)\s+and\s+restated\b", re.IGNORECASE),
    ],
    "realized_gain_loss": [
        re.compile(r"\brealized\s+gains?\b", re.IGNORECASE),
        re.compile(r"\brealized\s+loss(?:es)?\b", re.IGNORECASE),
        re.compile(r"\b(exit|exited|disposition|sold)\b", re.IGNORECASE),
    ],
}


def _load_universe(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for b in raw.get("bdcs") or []:
        cik = b.get("cik")
        if cik:
            out[cik] = b
    return out


def _normalize_doc(raw: str) -> str:
    txt = COMMENT_RE.sub(" ", raw)
    txt = SCRIPT_STYLE_RE.sub(" ", txt)
    txt = TAG_RE.sub(" ", txt)
    txt = txt.replace("\u00a0", " ")
    return WS_RE.sub(" ", txt).strip()


def _read_text(path: Path) -> str:
    blob = path.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            return blob.decode(enc, errors="ignore")
        except Exception:
            continue
    return ""


def _extract_items(text: str, snippet_chars: int = 220) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for m in ITEM_RE.finditer(text):
        item = m.group(1)
        start = max(0, m.start() - snippet_chars)
        end = min(len(text), m.end() + snippet_chars)
        key = (item, start)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "item": item,
            "char_start": m.start(),
            "char_end": m.end(),
            "snippet": text[start:end].strip(),
        })
    return out


def _flag_signals(text: str, items: list[dict]) -> dict:
    item_set = {x["item"] for x in items}
    flags: dict[str, bool] = {}
    hits: dict[str, list[dict]] = {}
    for key, patterns in KEYWORDS.items():
        found = []
        for p in patterns:
            m = p.search(text)
            if m:
                found.append({
                    "pattern": p.pattern,
                    "excerpt": text[max(0, m.start() - 120): m.end() + 120].strip(),
                })
        hits[key] = found
        flags[key] = bool(found)

    # Commonly related 8-K items for debt agreement changes.
    if "1.01" in item_set or "2.03" in item_set:
        flags["credit_facility_change"] = True

    return {"flags": flags, "keyword_hits": hits}


def _iter_candidate_8k(cik: str, filings_root: Path) -> list[dict]:
    cik_dir = filings_root / cik
    if not cik_dir.is_dir():
        return []
    rows = []
    for acc in cik_dir.iterdir():
        meta_p = acc / "meta.json"
        if not meta_p.exists():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (meta.get("form") or "").upper() not in SUPPORTED_FORMS:
            continue
        if not meta.get("primary_saved"):
            continue
        p = meta.get("local_primary_path")
        if not p or not Path(p).exists():
            continue
        rows.append(meta)
    rows.sort(key=lambda m: (m.get("filing_date") or m.get("report_date") or ""), reverse=True)
    return rows


def process_one(meta: dict, out_root: Path, force: bool = False) -> dict:
    cik = meta.get("cik")
    accn = (meta.get("accession_number") or "").replace("-", "")
    primary_path = Path(meta["local_primary_path"])
    target_dir = out_root / cik / "events8k" / accn
    target_dir.mkdir(parents=True, exist_ok=True)

    out_json = target_dir / "events_8k.json"
    if out_json.exists() and not force:
        return {"skipped": True, "reason": "already extracted", "path": str(out_json)}

    raw = _read_text(primary_path)
    text = _normalize_doc(raw)
    items = _extract_items(text)
    signals = _flag_signals(text, items)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["item"]] = counts.get(it["item"], 0) + 1

    payload = {
        "schema_version": "pricredit.events8k/v0",
        "cik": cik,
        "ticker": None,
        "company_name": None,
        "accession_number": meta.get("accession_number"),
        "form": meta.get("form"),
        "filing_date": meta.get("filing_date"),
        "report_date": meta.get("report_date"),
        "items": items,
        "item_counts": counts,
        "signals": signals,
        "source_primary_document": meta.get("primary_document"),
        "source_primary_url": meta.get("primary_url"),
        "source_local_primary_path": str(primary_path),
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": (
            "Minimal regex-based 8-K extraction from SEC EDGAR primary documents. "
            "Heuristic only; verify against original filing before decision use."
        ),
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (target_dir / "source.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"skipped": False, "path": str(out_json), "items": len(items)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--filings", default=str(DEFAULT_FILINGS),
                    help="Root of fetched filings (default: filings/).")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="Root for extracted outputs (default: extracted/).")
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE),
                    help="Path to bdc_universe.json for ticker/cik metadata.")
    ap.add_argument("--cik", default=DEFAULT_CIK,
                    help=f"Target CIK (default: {DEFAULT_CIK}, ARCC).")
    ap.add_argument("--ticker", default=DEFAULT_TICKER,
                    help=f"Target ticker label (default: {DEFAULT_TICKER}).")
    ap.add_argument("--limit", type=int, default=6,
                    help="Max 8-K filings to process (default: 6).")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild outputs even if events_8k.json exists.")
    ap.add_argument("--print", action="store_true",
                    help="Print a compact JSON run summary to stdout.")
    args = ap.parse_args()

    cik = str(args.cik).strip().zfill(10)
    filings_root = Path(args.filings)
    out_root = Path(args.out)
    universe = _load_universe(Path(args.universe))
    bdc = universe.get(cik, {})

    metas = _iter_candidate_8k(cik, filings_root)
    if args.limit > 0:
        metas = metas[:args.limit]
    if not metas:
        print(f"[events8k] no 8-K filings found for cik={cik}", flush=True)
        return 0

    processed = 0
    skipped = 0
    rows = []
    for meta in metas:
        res = process_one(meta, out_root, force=args.force)
        if res.get("skipped"):
            skipped += 1
        else:
            processed += 1
        rows.append({
            "accession_number": meta.get("accession_number"),
            "form": meta.get("form"),
            "filing_date": meta.get("filing_date"),
            **res,
        })

    summary = {
        "schema_version": "pricredit.events8k_run/v0",
        "cik": cik,
        "ticker": bdc.get("primary_ticker") or args.ticker,
        "company_name": bdc.get("company_name"),
        "processed": processed,
        "skipped": skipped,
        "attempted": len(metas),
        "rows": rows,
        "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    run_date = time.strftime("%Y-%m-%d", time.gmtime())
    reports_dir = ROOT / "reports" / run_date
    reports_dir.mkdir(parents=True, exist_ok=True)
    summary_path = reports_dir / f"events8k_summary_{summary['ticker']}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"[events8k] cik={cik} attempted={summary['attempted']} "
        f"processed={processed} skipped={skipped} summary={summary_path}",
        flush=True,
    )
    if args.print:
        print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
