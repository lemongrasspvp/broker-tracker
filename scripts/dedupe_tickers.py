#!/usr/bin/env python3
"""
scripts/dedupe_tickers.py — One-shot migration to consolidate duplicate
ticker entries across data files.

The analyzer historically stored stocks under whatever ticker variant
Claude happened to extract that day (e.g. "NOVO", "NOVO.CO", "NOVO-B.CO"
all referred to the same security). This bloated the dashboard with
duplicate cards.

This script:
  1. Scans analyses/*.json once to learn the (ticker → country) mapping
     Claude has used historically.
  2. Re-keys data/memory.json, data/broker_track.json, data/reeval.json
     under canonical tickers via enricher.canonical_ticker().
  3. Writes back atomically (*.tmp + rename) and prints what merged where.

Idempotent — running twice on already-canonical data is a no-op.
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Allow `from enricher import ...` when run from anywhere
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from enricher import canonical_ticker, guess_country, resolve_ticker  # noqa: E402

NORDIC = ("NO", "SE", "DK", "FI")
_SHARE_CLASS_RE = re.compile(r"-[A-D]$")

DATA_DIR     = ROOT / "data"
ANALYSES_DIR = ROOT / "analyses"
MEMORY_FILE  = DATA_DIR / "memory.json"
TRACKER_FILE = DATA_DIR / "broker_track.json"
REEVAL_FILE  = DATA_DIR / "reeval.json"


# ── Country detection ─────────────────────────────────────────────────────────

def build_country_map() -> dict:
    """Scan analyses/*.json for the (ticker → country) Claude tagged historically.

    Returns {ticker_upper: country} using the most common country per ticker.
    """
    if not ANALYSES_DIR.exists():
        return {}

    seen: dict[str, Counter] = {}
    files = sorted(p for p in ANALYSES_DIR.glob("*.json"))
    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for stock in data.get("stocks", []):
            t = (stock.get("ticker") or "").upper().strip()
            c = stock.get("country")
            if t and c:
                seen.setdefault(t, Counter())[c] += 1

    return {t: counter.most_common(1)[0][0] for t, counter in seen.items()}


def cluster_base(ticker: str) -> str:
    """Aggressive base for grouping: strip exchange suffix, compound, and share class.

    "NOVO", "NOVO.CO", "NOVO-B.CO" all collapse to "NOVO" so they cluster together.
    """
    base = resolve_ticker(ticker, "INTL")  # strips suffix + compound + prefix
    if _SHARE_CLASS_RE.search(base) and len(base) - 2 >= 3:
        base = base[:-2]
    return base


def country_for(raw_ticker: str, country_map: dict,
                base_clusters: dict | None = None,
                entry_counts: dict | None = None) -> str:
    """
    Best-effort country for a ticker. Priority:
      1. Definitive Nordic suffix on the ticker itself (.OL/.ST/.CO/.HE)
      2. Suffix on a Nordic sibling in the same base cluster (e.g. bare
         "BRUT" inherits NO from sibling "BRUT.OL"). Weighted by entry count.
      3. Bare 1-5 letter base → US (NVO, AAPL, MSFT — overrides Claude tags
         that may classify by company HQ rather than by listing exchange).
      4. Country tagged in the analyses (excluding INTL).
      5. Fallback to guess_country on the input.
    """
    upper = raw_ticker.upper().strip()
    base = cluster_base(upper)

    g = guess_country(upper)
    if g in NORDIC:
        return g

    if base_clusters is not None:
        sibling_votes: Counter = Counter()
        for sib in base_clusters.get(base, []):
            if sib.upper().strip() == upper:
                continue
            sg = guess_country(sib)
            if sg in NORDIC:
                weight = (entry_counts or {}).get(sib, 1)
                sibling_votes[sg] += weight
        if sibling_votes:
            return sibling_votes.most_common(1)[0][0]

    # Bare 1-5 letter ticker is almost always a US listing (NVO, NOD-pre-suffix
    # would have been caught by sibling check above; if no siblings exist, the
    # ticker really is a bare US ticker like NVO).
    if re.match(r"^[A-Z]{1,5}$", base):
        return "US"

    tagged = country_map.get(upper) or country_map.get(base)
    if tagged and tagged != "INTL":
        return tagged
    return g


# ── File I/O ──────────────────────────────────────────────────────────────────

def atomic_write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ── Migrations ────────────────────────────────────────────────────────────────

def migrate_memory(country_map: dict) -> dict:
    """Re-key memory.json. Returns {old_ticker: canonical_ticker} mapping."""
    if not MEMORY_FILE.exists():
        print(f"[skip] {MEMORY_FILE} not found")
        return {}

    memory = json.loads(MEMORY_FILE.read_text())

    # Pre-compute base clusters + entry counts so country_for can use sibling
    # suffixes when a bare ticker has no definitive country signal of its own.
    base_clusters: dict[str, list[str]] = {}
    entry_counts: dict[str, int] = {}
    for k, entries in memory.items():
        base_clusters.setdefault(cluster_base(k), []).append(k)
        entry_counts[k] = len(entries)

    mapping: dict[str, str] = {}
    new_memory: dict[str, list] = {}
    clusters: dict[str, list[tuple[str, int]]] = {}

    for old, entries in memory.items():
        country = country_for(old, country_map, base_clusters, entry_counts)
        canonical = canonical_ticker(old, country)
        mapping[old] = canonical
        new_memory.setdefault(canonical, []).extend(entries)
        clusters.setdefault(canonical, []).append((old, len(entries)))

    # Sort each merged list chronologically so dashboard's "latest" pick stays correct
    for canonical, entries in new_memory.items():
        entries.sort(key=lambda e: (e.get("date", ""), e.get("_analyzed_at", "")))
        # Keep last 20 to match memory.store_analysis's cap
        if len(entries) > 20:
            new_memory[canonical] = entries[-20:]

    print("=== memory.json ===")
    merged = 0
    for canonical, members in sorted(clusters.items()):
        if len(members) > 1:
            members_str = ", ".join(f"{m[0]!r} ({m[1]})" for m in members)
            total = sum(m[1] for m in members)
            print(f"  {canonical}  ←  {members_str}   = {total} entries")
            merged += 1

    print(f"  → {len(memory)} keys before, {len(new_memory)} keys after ({merged} clusters merged)")
    atomic_write_json(MEMORY_FILE, new_memory)
    return mapping


def migrate_tracker(mapping: dict, country_map: dict):
    """Rewrite each broker_track record's ticker field to canonical form."""
    if not TRACKER_FILE.exists():
        print(f"[skip] {TRACKER_FILE} not found")
        return

    records = json.loads(TRACKER_FILE.read_text())
    print("\n=== broker_track.json ===")

    rewritten = 0
    for r in records:
        old = (r.get("ticker") or "").strip()
        canonical = mapping.get(old)
        if canonical is None:
            canonical = canonical_ticker(old, country_for(old, country_map))
        if canonical and canonical != old:
            r["ticker"] = canonical
            rewritten += 1

    print(f"  → rewrote {rewritten}/{len(records)} record tickers")
    atomic_write_json(TRACKER_FILE, records)


def migrate_reeval(mapping: dict, country_map: dict):
    """Pick freshest entry per canonical key (history is per-key time-series)."""
    if not REEVAL_FILE.exists():
        print(f"[skip] {REEVAL_FILE} not found")
        return

    reeval = json.loads(REEVAL_FILE.read_text())
    print("\n=== reeval.json ===")

    # Group variants under their canonical key
    groups: dict[str, list[tuple[str, dict]]] = {}
    for old, entry in reeval.items():
        canonical = mapping.get(old)
        if canonical is None:
            canonical = canonical_ticker(old, country_for(old, country_map))
        groups.setdefault(canonical, []).append((old, entry))

    def freshness(entry: dict) -> str:
        """Latest date in history, falling back to empty string."""
        hist = entry.get("history") or []
        if not hist:
            return ""
        return max((h.get("date", "") for h in hist), default="")

    new_reeval: dict[str, dict] = {}
    merged = 0
    for canonical, members in sorted(groups.items()):
        if len(members) > 1:
            members.sort(key=lambda m: freshness(m[1]), reverse=True)
            kept_old, kept_entry = members[0]
            dropped = ", ".join(repr(m[0]) for m in members[1:])
            print(f"  {canonical}  ←  kept {kept_old!r} (freshest), dropped {dropped}")
            merged += 1
            new_reeval[canonical] = kept_entry
        else:
            new_reeval[canonical] = members[0][1]

    print(f"  → {len(reeval)} keys before, {len(new_reeval)} keys after ({merged} clusters merged)")
    atomic_write_json(REEVAL_FILE, new_reeval)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print("Scanning analyses/ for ticker→country mapping...")
    country_map = build_country_map()
    print(f"  Learned country for {len(country_map)} ticker(s)\n")

    mapping = migrate_memory(country_map)
    migrate_tracker(mapping, country_map)
    migrate_reeval(mapping, country_map)

    print("\nDone. Run `python3 dashboard.py` to regenerate the dashboard.")


if __name__ == "__main__":
    main()
