"""
backfill_missing_icons.py

Pure-stdlib Python tool for guessing icon URLs for AssetIndex entries that
have an empty IconUrl. Multi-pass, no LLM calls, optional HTTP probes.

Pipeline:
  Pass 1   Normalize-then-exact match against the known-good icon pool
  Pass 2   HTTP HEAD probe well-known item-icon CDNs
  Pass 3   Token-overlap (Jaccard) scoring against the pool
  Pass 4   Manual-review queue for whatever's still empty

Output:   tools/proposed_icon_fixes.json    { ShortName -> IconUrl }
          tools/manual_review.txt           low-confidence suggestions
The fixes file is read by convert_asset_index.py as a third icon-source layer.
This script never modifies AssetIndex.json or Entity_list.json directly.

Usage:
    python backfill_missing_icons.py
    python backfill_missing_icons.py --skip-http
    python backfill_missing_icons.py --workers 8 --timeout 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent

DEFAULT_INDEX  = str(_TOOLS_DIR / "AssetIndex.json")
DEFAULT_OUTPUT = str(_TOOLS_DIR / "proposed_icon_fixes.json")
DEFAULT_REVIEW = str(_TOOLS_DIR / "manual_review.txt")

# CDN templates probed in order. Lowercase shortname is appended to the path.
CDN_TEMPLATES = [
    "https://cdn.carbonmod.gg/items/{shortname}.png",
    "https://wiki.rustclash.com/img/items180/{shortname}.png",
]

USER_AGENT = "SandboxIconBackfill/1.0 (+local dev tool)"

SUFFIXES_TO_STRIP = [
    "_deployed", ".deployed", ".entity", "_static", ".static",
    ".worldmodel", ".viewmodel", "_collectable", "_collectible",
    "-collectable", "-collectible",
]

PREFIXES_TO_STRIP = [
    "halloween_", "halloween-",
    "christmas_", "christmas-", "xmas_", "xmas-",
    "easter_", "easter-",
    "lny_", "lny26_",
    "birthday_",
    "arctic_",
    "industrial_",
]

TOKEN_STOPWORDS = {
    "deployed", "static", "entity", "corpse", "worldmodel", "viewmodel",
    "prefab", "spawner", "collectable", "collectible",
    "the", "and", "of", "a", "an",
}

# Pass-3 thresholds
JACCARD_AUTO_ACCEPT = 0.8
JACCARD_REVIEW_MIN  = 0.5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CAMEL_BOUNDARY_1 = re.compile(r"([a-z0-9])([A-Z])")
CAMEL_BOUNDARY_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
TOKEN_SPLIT     = re.compile(r"[_.\-/\s]+")


def split_camel(s: str) -> str:
    s = CAMEL_BOUNDARY_1.sub(r"\1_\2", s)
    s = CAMEL_BOUNDARY_2.sub(r"\1_\2", s)
    return s


def normalize(s: str) -> str:
    """Aggressive normalize for Pass-1 exact-match lookup."""
    s = split_camel(s).lower()
    changed = True
    while changed:
        changed = False
        for sfx in SUFFIXES_TO_STRIP:
            if s.endswith(sfx):
                s = s[: -len(sfx)]
                changed = True
        for pfx in PREFIXES_TO_STRIP:
            if s.startswith(pfx):
                s = s[len(pfx):]
                changed = True
    # Collapse repeated separators
    s = re.sub(r"[_.\-]+", "_", s).strip("_")
    return s


def tokenize(s: str) -> set[str]:
    """Whole-token split for Pass-3 Jaccard scoring."""
    s = split_camel(s).lower()
    tokens = TOKEN_SPLIT.split(s)
    return {t for t in tokens if t and len(t) >= 2 and t not in TOKEN_STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# HTTP probe
# ---------------------------------------------------------------------------

def http_head_ok(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def probe_cdn_for_shortnames(shortnames: list[str], template: str,
                             workers: int, timeout: float, label: str) -> dict[str, str]:
    """Returns {shortname -> url} for any HEAD that returned 2xx/3xx."""
    found: dict[str, str] = {}
    total = len(shortnames)
    if total == 0:
        return found

    def attempt(sn: str) -> tuple[str, str | None]:
        url = template.format(shortname=urllib.parse.quote(sn.lower(), safe="-_."))
        return (sn, url if http_head_ok(url, timeout) else None)

    print(f"  [{label}] probing {total} shortnames with {workers} workers...")
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sn, url in ex.map(attempt, shortnames):
            completed += 1
            if completed % 100 == 0 or completed == total:
                print(f"    {completed}/{total}  hits so far: {len(found)}")
            if url is not None:
                found[sn] = url
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index",   default=DEFAULT_INDEX)
    ap.add_argument("--output",  default=DEFAULT_OUTPUT)
    ap.add_argument("--review",  default=DEFAULT_REVIEW)
    ap.add_argument("--skip-http", action="store_true",
                    help="Skip Pass 2 (no network).")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    index_path  = Path(args.index)
    output_path = Path(args.output)
    review_path = Path(args.review)

    if not index_path.is_file():
        print(f"ERROR: AssetIndex not found: {index_path}", file=sys.stderr)
        return 1

    entries = json.load(index_path.open(encoding="utf-8"))
    print(f"Loaded {len(entries)} entries from {index_path}")

    # --- Build candidate pool: every entry with a non-empty IconUrl ---
    pool: list[tuple[str, str, str, set[str]]] = []  # (shortname, normalized, iconurl, tokens)
    for e in entries:
        url = (e.get("IconUrl") or "").strip()
        if not url:
            continue
        sn = e["ShortName"]
        pool.append((sn, normalize(sn), url, tokenize(sn)))
    print(f"Candidate pool size (entries with icons): {len(pool)}")

    # Index pool by normalized name for O(1) Pass-1 lookup
    pool_by_norm: dict[str, str] = {}
    for sn, norm, url, _toks in pool:
        # First-write wins; keep the most "canonical" name
        pool_by_norm.setdefault(norm, url)

    # --- Find missing entries ---
    missing = [e for e in entries if not (e.get("IconUrl") or "").strip()]
    print(f"Missing icons: {len(missing)}")
    if not missing:
        print("Nothing to backfill.")
        return 0

    fixes: dict[str, str] = {}     # shortname -> url
    fix_source: dict[str, str] = {} # shortname -> "pass1" | "carbonmod" | "rustclash" | "jaccard"

    # =====================================================================
    # PASS 1 - normalize-then-exact match
    # =====================================================================
    pass1_hits = 0
    for e in missing:
        sn = e["ShortName"]
        norm = normalize(sn)
        url = pool_by_norm.get(norm)
        if url:
            fixes[sn] = url
            fix_source[sn] = "pass1"
            pass1_hits += 1
    print(f"Pass 1 (normalize+exact)            : {pass1_hits} fixed")

    still_missing = [e for e in missing if e["ShortName"] not in fixes]

    # =====================================================================
    # PASS 2 - HTTP HEAD probe of CDNs
    # =====================================================================
    pass2_hits_per_cdn: list[tuple[str, int]] = []
    if args.skip_http:
        print("Pass 2 (HTTP CDN probe)              : SKIPPED (--skip-http)")
    else:
        remaining_sn = [e["ShortName"] for e in still_missing]
        for template in CDN_TEMPLATES:
            label = template.split("//", 1)[1].split("/", 1)[0]  # host
            found = probe_cdn_for_shortnames(remaining_sn, template,
                                             workers=args.workers,
                                             timeout=args.timeout,
                                             label=label)
            for sn, url in found.items():
                if sn not in fixes:
                    fixes[sn] = url
                    fix_source[sn] = label
            pass2_hits_per_cdn.append((label, len(found)))
            remaining_sn = [sn for sn in remaining_sn if sn not in fixes]
            if not remaining_sn:
                break
        total_p2 = sum(c for _, c in pass2_hits_per_cdn)
        breakdown = "  ".join(f"{lbl}={c}" for lbl, c in pass2_hits_per_cdn)
        print(f"Pass 2 (HTTP CDN probe)              : {total_p2} fixed   [{breakdown}]")

    still_missing = [e for e in missing if e["ShortName"] not in fixes]

    # =====================================================================
    # PASS 3 - Token Jaccard scoring
    # =====================================================================
    pass3_hits = 0
    review_rows: list[tuple[str, str, str, list[tuple[float, str, str]]]] = []
    # (category, shortname, prefab_path, [(score, candidate_sn, candidate_url), ...])

    pool_tokens = [(sn, toks, url) for sn, _norm, url, toks in pool if toks]

    for e in still_missing:
        sn = e["ShortName"]
        toks = tokenize(sn)
        if not toks:
            continue

        # Pre-filter: only score candidates that share at least 1 token
        scored: list[tuple[float, str, str]] = []
        for cand_sn, cand_toks, cand_url in pool_tokens:
            if not (toks & cand_toks):
                continue
            score = jaccard(toks, cand_toks)
            if score >= JACCARD_REVIEW_MIN:
                scored.append((score, cand_sn, cand_url))

        scored.sort(key=lambda r: -r[0])
        if not scored:
            continue

        top_score = scored[0][0]
        if top_score >= JACCARD_AUTO_ACCEPT:
            fixes[sn] = scored[0][2]
            fix_source[sn] = "jaccard"
            pass3_hits += 1
        else:
            review_rows.append((e["Category"], sn, e["PrefabPath"], scored[:3]))
    print(f"Pass 3 (Jaccard auto-accept >={JACCARD_AUTO_ACCEPT})  : {pass3_hits} fixed")

    # =====================================================================
    # PASS 4 - Write manual review queue
    # =====================================================================
    still_missing = [e for e in missing if e["ShortName"] not in fixes]
    if review_rows or still_missing:
        with review_path.open("w", encoding="utf-8") as f:
            f.write(f"# Manual review queue\n")
            f.write(f"# {len(review_rows)} entries with low/medium-confidence "
                    f"Jaccard suggestions (score in [{JACCARD_REVIEW_MIN}, {JACCARD_AUTO_ACCEPT})).\n")
            f.write(f"# {len(still_missing) - len(review_rows)} entries with no candidate at all.\n")
            f.write("# Format per row:\n")
            f.write("#   [Category] ShortName  ->  PrefabPath\n")
            f.write("#       <score> <candidate-shortname>   <candidate-url>\n\n")

            review_rows.sort(key=lambda r: (r[0], r[1].lower()))
            for cat, sn, pp, scored in review_rows:
                f.write(f"[{cat}] {sn}  ->  {pp}\n")
                for score, csn, curl in scored:
                    f.write(f"    {score:.2f}  {csn:<40}  {curl}\n")
                f.write("\n")

            if len(still_missing) > len(review_rows):
                f.write("\n## Entries with no token-overlap candidates (no suggestion):\n\n")
                no_cand = [e for e in still_missing
                           if not any(r[1] == e["ShortName"] for r in review_rows)]
                no_cand.sort(key=lambda e: (e["Category"], e["ShortName"].lower()))
                for e in no_cand:
                    f.write(f"  [{e['Category']}] {e['ShortName']}  ->  {e['PrefabPath']}\n")
        print(f"Pass 4: wrote review queue ({len(review_rows)} suggestions, "
              f"{len(still_missing) - len(review_rows)} no-candidates) -> {review_path}")
    elif review_path.exists():
        review_path.unlink()

    # =====================================================================
    # Write fixes file
    # =====================================================================
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_fixes = dict(sorted(fixes.items(), key=lambda kv: kv[0].lower()))
    output_path.write_text(json.dumps(sorted_fixes, indent=2), encoding="utf-8")

    # Source-attribution sidecar (informational only - not consumed by C# or by
    # convert_asset_index.py; useful for auditing where each fix came from)
    sources_path = output_path.with_name(output_path.stem + "_sources.json")
    sources_payload = {sn: fix_source[sn] for sn in sorted_fixes.keys()}
    sources_path.write_text(json.dumps(sources_payload, indent=2), encoding="utf-8")

    # =====================================================================
    # Final report
    # =====================================================================
    print("=" * 70)
    print(f"Total missing            : {len(missing)}")
    print(f"Fixed                    : {len(fixes)}")
    print(f"Still missing            : {len(missing) - len(fixes)}")
    print(f"  -> in review queue     : {len(review_rows)}")
    print(f"  -> no candidate at all : {len(missing) - len(fixes) - len(review_rows)}")
    src_breakdown: Counter[str] = Counter(fix_source.values())
    print("Fix sources:")
    for src, n in src_breakdown.most_common():
        print(f"  {src:<14} {n}")
    print(f"Wrote: {output_path}")
    print(f"Wrote: {sources_path}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
