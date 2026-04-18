"""
Inline-XBRL (iXBRL) parsing primitives for BDC Schedule-of-Investments.

BDC primary documents (*.htm for 10-K / 10-Q) embed XBRL facts inline
inside HTML. We can't use a standard XML parser — the files are 5-75 MB
and aren't namespaced well enough for a DOM parse — so we use a careful
regex pass that only scans for:

  1. `<xbrli:context id="...">...</xbrli:context>` blocks, which carry
     the period and the per-axis dimension members we need.
  2. `<ix:nonFraction name="..." contextRef="..." scale="..." ...>NNN</ix:nonFraction>`
     numeric facts, which carry the data (SoI fair values, costs, etc.).

Per-investment data in BDC SoIs is filed under a small set of common
axes, validated across 52 cached BDC filings:

  us-gaap:EquitySecuritiesByIndustryAxis   (96% coverage)
  us-gaap:InvestmentTypeAxis               (100%)
  us-gaap:InvestmentIdentifierAxis         (96%)
  us-gaap:InvestmentIssuerAffiliationAxis  (affiliate classification)

These are mutually-exclusive axes on most filers: each context pins
exactly one axis member, and the filer declares the corresponding
subtotal fact. That means we can aggregate by industry, by investment
type, or by issuer, by simply summing facts grouped by axis member —
no need to build the full cross-product of per-line leaves.

Caveat: non-accrual disclosure is NOT reliably tagged across BDCs.
Only ~4% (ARCC, CION) publish a direct top-level
`InvestmentOwned…NonAccrualStatusPercentOfFairValue` concept. For the
remaining 96% the figure lives in footnote text and needs either a
Schedule-of-Investments narrative parser or a manual attestation.
v0 simply emits null for filers that don't tag the fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Axis identifiers — sorted so we can deterministically pick the "primary"
# axis when a context carries more than one.
# ---------------------------------------------------------------------------

AXIS_INDUSTRY = "us-gaap:EquitySecuritiesByIndustryAxis"
AXIS_INVESTMENT_TYPE = "us-gaap:InvestmentTypeAxis"
AXIS_INVESTMENT_IDENTIFIER = "us-gaap:InvestmentIdentifierAxis"
AXIS_ISSUER_AFFILIATION = "us-gaap:InvestmentIssuerAffiliationAxis"

# ---------------------------------------------------------------------------
# Regexes — compiled once.
# ---------------------------------------------------------------------------

_CONTEXT_RE = re.compile(
    r'<xbrli:context[^>]*\bid="([^"]+)"[^>]*>(.*?)</xbrli:context>',
    re.DOTALL,
)

_INSTANT_RE = re.compile(r"<xbrli:instant>([^<]+)</xbrli:instant>")
_START_RE = re.compile(r"<xbrli:startDate>([^<]+)</xbrli:startDate>")
_END_RE = re.compile(r"<xbrli:endDate>([^<]+)</xbrli:endDate>")
_MEMBER_RE = re.compile(
    r'<xbrldi:explicitMember\s+dimension="([^"]+)"[^>]*>([^<]+)</xbrldi:explicitMember>'
)

_FACT_RE = re.compile(
    r'<ix:nonFraction\b([^>]*?)>([^<]+)</ix:nonFraction>',
    re.DOTALL,
)

_ATTR_NAME_RE = re.compile(r'\bname="([^"]+)"')
_ATTR_CONTEXT_RE = re.compile(r'\bcontextRef="([^"]+)"')
_ATTR_SCALE_RE = re.compile(r'\bscale="(-?\d+)"')
_ATTR_UNIT_RE = re.compile(r'\bunitRef="([^"]+)"')
_ATTR_SIGN_RE = re.compile(r'\bsign="(-)"')
_ATTR_FORMAT_RE = re.compile(r'\bformat="([^"]+)"')


# ---------------------------------------------------------------------------
# Data classes — lightweight, intentionally no external deps.
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """An XBRL context: period + axis members."""
    id: str
    period_end: Optional[str] = None
    period_start: Optional[str] = None
    members: dict[str, str] = field(default_factory=dict)


@dataclass
class Fact:
    """A single numeric fact pointing at a context."""
    concept: str
    context_ref: str
    unit: Optional[str]
    value: float       # already scale-adjusted
    raw_text: str      # original text for debugging


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

def parse_contexts(ixbrl_text: str) -> dict[str, Context]:
    """Return {context_id: Context}."""
    out: dict[str, Context] = {}
    for m in _CONTEXT_RE.finditer(ixbrl_text):
        ctx_id, body = m.group(1), m.group(2)
        ctx = Context(id=ctx_id)
        inst = _INSTANT_RE.search(body)
        if inst:
            ctx.period_end = inst.group(1).strip()
        else:
            start = _START_RE.search(body)
            end = _END_RE.search(body)
            if start:
                ctx.period_start = start.group(1).strip()
            if end:
                ctx.period_end = end.group(1).strip()
        for mm in _MEMBER_RE.finditer(body):
            ctx.members[mm.group(1)] = mm.group(2).strip()
        out[ctx_id] = ctx
    return out


def _coerce_number(raw: str, scale: int, is_negative: bool) -> Optional[float]:
    """iXBRL numeric fact -> scaled Python float."""
    cleaned = raw.replace(",", "").replace("\xa0", "").strip()
    if not cleaned:
        return None
    # Some filers wrap values in parentheses to denote negatives.
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
        is_negative = True
    try:
        v = float(cleaned)
    except ValueError:
        return None
    v *= 10 ** scale
    if is_negative:
        v = -v
    return v


def iter_facts(ixbrl_text: str, concepts: Optional[set[str]] = None) -> Iterator[Fact]:
    """Yield every `<ix:nonFraction>` fact (optionally filtered by concept).

    The caller-filtered version is much faster than building every fact and
    discarding — important because a 25MB BDC 10-K has ~5k facts.
    """
    for m in _FACT_RE.finditer(ixbrl_text):
        attrs, inner = m.group(1), m.group(2)
        nm = _ATTR_NAME_RE.search(attrs)
        if not nm:
            continue
        concept = nm.group(1)
        if concepts is not None and concept not in concepts:
            continue
        ctx = _ATTR_CONTEXT_RE.search(attrs)
        if not ctx:
            continue
        scale_m = _ATTR_SCALE_RE.search(attrs)
        scale = int(scale_m.group(1)) if scale_m else 0
        sign_m = _ATTR_SIGN_RE.search(attrs)
        is_neg = sign_m is not None
        unit_m = _ATTR_UNIT_RE.search(attrs)
        val = _coerce_number(inner, scale, is_neg)
        if val is None:
            continue
        yield Fact(
            concept=concept,
            context_ref=ctx.group(1),
            unit=unit_m.group(1) if unit_m else None,
            value=val,
            raw_text=inner,
        )


# ---------------------------------------------------------------------------
# Convenience helpers on top.
# ---------------------------------------------------------------------------

def pick_latest_period(contexts: Iterable[Context]) -> Optional[str]:
    """Return the most recent `period_end` seen, ISO-lex sortable."""
    ends = [c.period_end for c in contexts if c.period_end]
    return max(ends) if ends else None


def _clean_member_label(member: str) -> str:
    """Strip `prefix:` and `...Member` suffix, CamelCase -> spaces."""
    label = member.split(":", 1)[-1]
    for suf in ("Member", "Industries", "Industry", "Sector"):
        if label.endswith(suf):
            label = label[: -len(suf)]
    # Insert a space between lowercase/uppercase transitions.
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)
    label = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", label)
    return label.strip() or member


def aggregate_fv_by_axis(
    facts: Iterable[Fact],
    contexts: dict[str, Context],
    axis: str,
    period_end: Optional[str] = None,
) -> dict[str, float]:
    """Sum fact values by axis member using *pure* (single-axis) contexts only.

    Used for the affiliation aggregate, where filers disclose exactly one
    subtotal per affiliation member with no cross-axis overlap.
    """
    out: dict[str, float] = {}
    for f in facts:
        ctx = contexts.get(f.context_ref)
        if not ctx:
            continue
        if period_end and ctx.period_end != period_end:
            continue
        member = ctx.members.get(axis)
        if not member:
            continue
        other_axes = [a for a in ctx.members if a != axis]
        if other_axes:
            continue
        out[member] = out.get(member, 0.0) + f.value
    return out


def aggregate_by_axis_best_signature(
    facts: Iterable[Fact],
    contexts: dict[str, Context],
    axis: str,
    period_end: Optional[str] = None,
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Group facts by context-axis signature, return the largest-total group.

    Many BDCs tag the Schedule of Investments at a leaf-level context
    that carries *multiple* axes (industry + affiliation + type, for
    example). The pure-mutex aggregator misses those. This helper picks
    the axis-signature under which `axis` accumulates the largest
    total — that's the filer's canonical per-industry (or per-type)
    decomposition.

    Returns ({member: $total}, the-winning-signature).
    If nothing matches, returns ({}, ()).
    """
    by_sig: dict[tuple[str, ...], dict[str, float]] = {}
    for f in facts:
        ctx = contexts.get(f.context_ref)
        if not ctx:
            continue
        if period_end and ctx.period_end != period_end:
            continue
        member = ctx.members.get(axis)
        if not member:
            continue
        sig = tuple(sorted(ctx.members.keys()))
        by_sig.setdefault(sig, {}).setdefault(member, 0.0)
        by_sig[sig][member] += f.value
    if not by_sig:
        return {}, ()
    best_sig = max(by_sig, key=lambda s: sum(by_sig[s].values()))
    return by_sig[best_sig], best_sig


def compute_hhi(shares: dict[str, float]) -> Optional[float]:
    """Herfindahl-Hirschman Index on raw $ values; returns 0..1."""
    total = sum(v for v in shares.values() if v > 0)
    if total <= 0:
        return None
    return sum((v / total) ** 2 for v in shares.values() if v > 0)


def top_n(
    shares: dict[str, float], n: int = 5, label_cleaner=_clean_member_label
) -> list[dict]:
    """Rank members by $, return up to `n` with cleaned labels and shares."""
    total = sum(v for v in shares.values() if v > 0) or 1.0
    ordered = sorted(((v, k) for k, v in shares.items() if v > 0), reverse=True)[:n]
    return [
        {"name": label_cleaner(k), "raw_member": k, "fair_value": v,
         "pct_portfolio": round(v / total * 100, 4)}
        for v, k in ordered
    ]


def load_primary(path: Path | str) -> str:
    """Read a primary document; tolerate encoding quirks in filer HTML."""
    p = Path(path)
    return p.read_text(errors="ignore")
