# EDGAR usage policy

SEC EDGAR is free and authoritative, but abuse triggers IP bans.
Everything in this repository goes through
[`scripts/_edgar_common.py`](../scripts/_edgar_common.py) to enforce:

1. **A real `User-Agent`**. SEC requires `Name email` — a bot prefix +
   contact address. We default to
   `PriCredit-AICRM/0.1 (${PRICREDIT_UA_EMAIL})` and refuse to run if
   `PRICREDIT_UA_EMAIL` is unset or clearly fake. See the official
   fair-use guidance:
   <https://www.sec.gov/os/webmaster-faq#code-support>.
2. **10 req/sec hard cap** (SEC's stated ceiling). Our client sleeps
   between requests so the effective rate stays ≤8 req/s, giving a
   safety margin.
3. **Retry with jitter** on `429 Too Many Requests` and `5xx`. Three
   attempts max, exponential backoff starting at 2 s.
4. **Local file cache** (`bdc/_cache/`, git-ignored). Any `GET` that
   maps to a stable EDGAR URL (company_tickers.json, submissions.json,
   archived filings) is memoized for 24 h by default; pass
   `--no-cache` in the CLIs to force a refresh.

## Endpoints we use

| Purpose | URL template |
|---------|--------------|
| Ticker → CIK map | `https://www.sec.gov/files/company_tickers.json` |
| Submissions per CIK | `https://data.sec.gov/submissions/CIK{cik:010d}.json` |
| XBRL company facts | `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json` |
| XBRL concept | `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json` |
| Filing index | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/` |
| Primary document | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{filename}` |
| Full-text search | `https://efts.sec.gov/LATEST/search-index?q={q}&forms={forms}` |

See the SEC's own index of machine-readable endpoints:
<https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>

## Do / don't

- **Do** serialize all EDGAR traffic through the shared client so the
  rate limiter is global.
- **Do** ship the CIK as a zero-padded 10-digit string for
  `data.sec.gov` endpoints, but un-padded for `www.sec.gov/Archives`.
- **Don't** mirror EDGAR. Pull only what a scenario asks for.
- **Don't** commit raw filings. They're public, but large, and easy
  to re-download.
- **Don't** parallelize across processes unless each process uses the
  same throttled session and your combined rate still ≤10/s.

## Operational tips

- Prefer `submissions.json` to enumerate a company's filings; it's
  cheap, cacheable, and contains accession numbers + primary document
  filenames you need to construct archive URLs.
- For numeric trends (NAV per share, NII, asset coverage), XBRL facts
  are ~10x less work than parsing 10-Q HTML. Use
  `companyfacts` first and fall back to text parsing only for
  concepts that aren't tagged (e.g., Schedule of Investments).
- Full-text search via EFTS is fantastic for "which BDCs disclosed a
  non-accrual of $ABC this quarter" but its results aren't definitive;
  always confirm against the underlying filing.
