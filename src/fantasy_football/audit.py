"""Junk-submission audit for the public picks inbox.

The commit Worker guarantees submissions are *well-formed* (key,number rows,
size caps); this module judges whether they look *legitimate*. It validates
each ``picks/*.csv`` against the newest committed master (no database or
network needed, so the arrival watchdog stays fast):

- **unknown keys** — most of the file's players don't exist in our pool;
- **off-scale ratings** — values far outside the master's rating scale;
- **degenerate content** — many rows, all with the identical rating;
- **sybil clones** — byte-equivalent rating sets submitted under several ids
  (one person manufacturing extra "votes");
- **volume tripwire** — the inbox holds suspiciously many files (bot flood).

Flagged files are excluded from the blend by moving them to
``picks/quarantine/`` (the rebuild only globs ``picks/*.csv``); nothing is
deleted, so false positives are one ``git mv`` away from rejoining.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

#: A file is junk-keyed when this share of its rows reference unknown players
#: (and at least MIN_UNKNOWN rows do, so tiny files can't trip on one typo).
UNKNOWN_RATIO = 0.4
MIN_UNKNOWN = 5
#: Ratings live on the production-value scale; allow generous slack around it.
SCALE_SLACK = 1.5
SCALE_RATIO = 0.3
#: "Everyone identical" only counts as degenerate with a real number of rows.
DEGENERATE_MIN_ROWS = 10
#: Inbox tripwire: the weekly archive clears picks/, so growth past this within
#: one cycle smells like a flood, not enthusiasm.
DEFAULT_MAX_FILES = 300


class Finding(NamedTuple):
    path: str
    reason: str


def _read_rows(path: str) -> list[tuple[str, float]]:
    """(key, rating) rows of a pick file; malformed rows are skipped (the
    Worker already rejects them, but files can also arrive by hand)."""
    import csv

    rows: list[tuple[str, float]] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("key") or "").strip()
            try:
                rating = float((row.get("rating") or "").strip())
            except ValueError:
                continue
            if key:
                rows.append((key, rating))
    return rows


def audit_pick_files(
    paths: list[str],
    master_ratings: dict[str, float],
    *,
    max_files: int = DEFAULT_MAX_FILES,
) -> list[Finding]:
    """Findings for every suspicious pick file (empty list = all clean)."""
    findings: list[Finding] = []
    known = set(master_ratings)
    top = max(master_ratings.values(), default=0.0)
    hi = top * SCALE_SLACK + 50.0
    lo = -50.0

    seen_hash: dict[str, str] = {}
    for path in sorted(paths):
        rows = _read_rows(path)
        if not rows:
            findings.append(Finding(path, "no parseable pick rows"))
            continue

        if known:
            unknown = [k for k, _ in rows if k not in known]
            if len(unknown) >= MIN_UNKNOWN and len(unknown) / len(rows) > UNKNOWN_RATIO:
                findings.append(Finding(
                    path, f"{len(unknown)}/{len(rows)} rows reference unknown players"))
                continue
            off = [r for _, r in rows if r < lo or r > hi]
            if len(off) / len(rows) > SCALE_RATIO:
                findings.append(Finding(
                    path, f"{len(off)}/{len(rows)} ratings far off the value scale "
                          f"(expected roughly {lo:.0f}..{hi:.0f})"))
                continue

        if len(rows) >= DEGENERATE_MIN_ROWS and len({r for _, r in rows}) == 1:
            findings.append(Finding(
                path, f"all {len(rows)} rows carry the identical rating"))
            continue

        # Sybil clones: same rating content under a different id. Hash the
        # sorted (key, rating) pairs so row order doesn't matter.
        digest = hashlib.sha256(
            "\n".join(f"{k},{r}" for k, r in sorted(rows)).encode()
        ).hexdigest()
        if digest in seen_hash:
            findings.append(Finding(
                path, f"identical content to {seen_hash[digest]} (sybil clone)"))
            continue
        seen_hash[digest] = path

    if len(paths) > max_files:
        findings.append(Finding(
            "picks/", f"inbox holds {len(paths)} files (tripwire {max_files}) - possible flood"))
    return findings
