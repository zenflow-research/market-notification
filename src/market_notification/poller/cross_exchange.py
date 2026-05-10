"""Cross-exchange grouping (FR-INGEST-005, FR-INGEST-006).

When the same company files an event with both BSE and NSE within
+/-10 minutes AND headlines are >=85% similar (cosine on tokens), they
are grouped under a shared `cross_exchange_group_id`. The second arrival
is marked `cross_exchange_role='duplicate_dropped'` and short-circuits
the rest of the pipeline.

Pure functions only -- no DB, no I/O. Caller is responsible for the
window query (last 10 minutes for the same company_id).
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

# Public knobs (reflected in PLAN.md / SPEC.md).
DEFAULT_WINDOW_MINUTES = 10
DEFAULT_SIMILARITY_THRESHOLD = 0.85

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric tokens. Strips punctuation, ticker codes survive."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def cosine_similarity(a: str, b: str) -> float:
    """Token-bag cosine similarity in [0, 1]. Returns 0.0 if either side empty."""
    ca = Counter(_tokenize(a))
    cb = Counter(_tokenize(b))
    if not ca or not cb:
        return 0.0
    common = set(ca) & set(cb)
    num = sum(ca[t] * cb[t] for t in common)
    den_a = math.sqrt(sum(v * v for v in ca.values()))
    den_b = math.sqrt(sum(v * v for v in cb.values()))
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def find_match(
    candidate: dict,
    existing_window: Iterable[dict],
    *,
    window: timedelta = timedelta(minutes=DEFAULT_WINDOW_MINUTES),
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> Optional[dict]:
    """Return the first existing row that matches `candidate` cross-exchange.

    `candidate` and rows in `existing_window` must be dicts with at least:
      - source: 'BSE' | 'NSE'
      - company_id: int
      - announced_at: datetime
      - headline: str

    Match conditions (all required):
      - same `company_id`
      - opposite `source` (BSE <-> NSE)
      - |announced_at delta| <= window
      - headline cosine >= threshold
    """
    cand_announced: datetime = candidate["announced_at"]
    cand_source: str = candidate["source"]
    cand_company: int = candidate["company_id"]
    cand_headline: str = candidate["headline"]

    for row in existing_window:
        if row.get("company_id") != cand_company:
            continue
        if row.get("source") == cand_source:
            continue  # same exchange -> not "cross"
        delta = abs(row["announced_at"] - cand_announced)
        if delta > window:
            continue
        if cosine_similarity(row["headline"], cand_headline) >= threshold:
            return row
    return None


def assign_role(match: Optional[dict]) -> tuple[str, str]:
    """Decide (group_id, role) for an incoming candidate.

    `match` is the FIRST-arrival row found by `find_match`, or None when no
    cross-exchange partner exists yet:

      - match is None  -> candidate is the first/only arrival; mint a fresh
        group_id and mark it 'primary'. A solo group is harmless.
      - match is set   -> candidate is the SECOND arrival; reuse the match's
        group_id (or mint one if it was absent) and mark 'duplicate_dropped'.
    """
    if match is None:
        return (str(uuid.uuid4()), "primary")
    group_id = match.get("cross_exchange_group_id") or str(uuid.uuid4())
    return (group_id, "duplicate_dropped")
