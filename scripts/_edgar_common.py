"""
Shared EDGAR HTTP client for PriCredit scripts.

Guarantees:
  * SEC-compliant User-Agent built from $PRICREDIT_UA_EMAIL
    (refuses to run with a missing / obviously fake value).
  * ~8 req/sec cap (SEC ceiling is 10/s — we leave headroom).
  * Automatic retry on 429 / 5xx with jittered exponential backoff.
  * On-disk JSON / bytes cache (default 24 h) keyed by URL; skip with
    `use_cache=False` or by deleting `bdc/_cache/`.

Usage:
    from _edgar_common import edgar_get_json, edgar_get_bytes, pad_cik
    data = edgar_get_json("https://data.sec.gov/submissions/CIK0000814052.json")
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "requests not installed. Run: pip install -r scripts/requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = ROOT / "bdc" / "_cache"
DEFAULT_CACHE_TTL_S = 24 * 60 * 60  # 24h
DEFAULT_MIN_INTERVAL_S = 1.0 / 8.0   # ≤8 req/sec (SEC allows up to 10)
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 30


_SESSION_LOCK = threading.Lock()
_SHARED_SESSION: Optional[requests.Session] = None
_LAST_CALL_AT: float = 0.0


class EdgarConfigError(RuntimeError):
    pass


def _contact_email() -> str:
    email = (os.environ.get("PRICREDIT_UA_EMAIL") or "").strip()
    if not email or "@" not in email or email.endswith("example.com"):
        raise EdgarConfigError(
            "PRICREDIT_UA_EMAIL is not set to a real contact address.\n"
            "SEC EDGAR requires a compliant User-Agent. Export it once:\n"
            "    export PRICREDIT_UA_EMAIL=you@example.org\n"
            "or add the line to ~/.pricredit-env."
        )
    return email


def user_agent() -> str:
    return f"PriCredit-AICRM/0.1 ({_contact_email()})"


def get_session() -> requests.Session:
    global _SHARED_SESSION
    with _SESSION_LOCK:
        if _SHARED_SESSION is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": user_agent(),
                "Accept-Encoding": "gzip, deflate",
                # data.sec.gov wants this; www.sec.gov ignores it.
                "Host-hint": "data.sec.gov",
            })
            _SHARED_SESSION = s
        return _SHARED_SESSION


def _throttle(min_interval: float = DEFAULT_MIN_INTERVAL_S) -> None:
    global _LAST_CALL_AT
    with _SESSION_LOCK:
        now = time.monotonic()
        delta = now - _LAST_CALL_AT
        if delta < min_interval:
            time.sleep(min_interval - delta)
        _LAST_CALL_AT = time.monotonic()


def _cache_path(url: str, cache_dir: Path) -> Path:
    parsed = urlparse(url)
    domain = (parsed.netloc or "unknown").replace(":", "_")
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    tail = (parsed.path.rsplit("/", 1)[-1] or "root")[:64]
    return cache_dir / domain / f"{tail}.{h}.bin"


def _cache_get(path: Path, ttl_s: int) -> Optional[bytes]:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl_s:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _cache_put(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def edgar_get_bytes(
    url: str,
    *,
    use_cache: bool = True,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT,
    min_interval: float = DEFAULT_MIN_INTERVAL_S,
    extra_headers: Optional[dict] = None,
) -> bytes:
    cache_path = _cache_path(url, cache_dir)
    if use_cache:
        cached = _cache_get(cache_path, cache_ttl_s)
        if cached is not None:
            return cached

    sess = get_session()
    headers = {"User-Agent": user_agent()}
    if extra_headers:
        headers.update(extra_headers)

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        _throttle(min_interval)
        try:
            r = sess.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            backoff = 2 ** attempt + random.random()
            print(f"[edgar] {url} network error {exc!r}; retry in {backoff:.1f}s",
                  file=sys.stderr)
            time.sleep(backoff)
            continue

        if r.status_code == 200:
            data = r.content
            if use_cache:
                _cache_put(cache_path, data)
            return data

        # 403 often means the User-Agent was rejected — stop retrying.
        if r.status_code == 403:
            raise EdgarConfigError(
                f"EDGAR returned 403 for {url}.\n"
                f"User-Agent in use: {headers['User-Agent']!r}\n"
                "Check that PRICREDIT_UA_EMAIL is a real address."
            )
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else (2 ** attempt + random.random())
            except ValueError:
                wait = 2 ** attempt + random.random()
            print(f"[edgar] {url} -> {r.status_code}; retry in {wait:.1f}s",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        # Any other status: raise immediately.
        r.raise_for_status()

    if last_exc:
        raise last_exc
    raise RuntimeError(f"EDGAR request failed after {retries} retries: {url}")


def edgar_get_json(url: str, **kw: Any) -> Any:
    blob = edgar_get_bytes(url, **kw)
    return json.loads(blob.decode("utf-8"))


def pad_cik(cik: int | str) -> str:
    """CIK as 10-digit zero-padded string (required by data.sec.gov)."""
    n = int(str(cik).lstrip("0") or "0")
    return f"{n:010d}"


def accession_no_dashes(acc: str) -> str:
    """'0000814052-25-000081' -> '000081405225000081'."""
    return acc.replace("-", "")


def archive_url(cik: int | str, accession: str, filename: str = "") -> str:
    """Build a www.sec.gov/Archives URL; CIK is un-padded there."""
    n = int(str(cik).lstrip("0") or "0")
    acc = accession_no_dashes(accession)
    base = f"https://www.sec.gov/Archives/edgar/data/{n}/{acc}"
    return f"{base}/{filename}" if filename else base + "/"


def submissions_url(cik: int | str) -> str:
    return f"https://data.sec.gov/submissions/CIK{pad_cik(cik)}.json"


def companyfacts_url(cik: int | str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pad_cik(cik)}.json"


def company_tickers_url() -> str:
    return "https://www.sec.gov/files/company_tickers.json"


# Small self-check hook used by the smoke-test in run-daily-pricredit.sh.
def preflight() -> None:
    _ = user_agent()  # raises EdgarConfigError on misconfig
    print(f"[edgar] UA ok: {user_agent()}", file=sys.stderr)


if __name__ == "__main__":
    preflight()
