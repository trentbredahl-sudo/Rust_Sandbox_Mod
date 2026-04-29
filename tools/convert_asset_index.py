"""
RustEdit asset-path index + Entity_list items  ->  Sandbox plugin AssetIndex.json

One-shot build-time tool. NOT shipped with the plugin.

Pass 1 (prefabs):  parses the flat list of PNG paths exported from RustEdit's
                   preview cache into spawnable prefab entries.
Pass 2 (items):    folds inventory items from Entity_list.json (shortname ->
                   icon URL, grouped by category) into the same output, with
                   IsItem=true so the C# plugin can branch on spawn.

Both passes write to a single AssetIndex.json with a unified schema:
    { ShortName, PrefabPath, Category, IconUrl, IsItem }

Usage:
    python convert_asset_index.py                     # uses defaults below
    python convert_asset_index.py --icon-base "https://..." --icon-layout nested
    python convert_asset_index.py --no-items          # skip Pass 2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (override via CLI args)
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).parent

DEFAULT_INPUT      = r"C:\RustModding\Icon Troubleshooting\RustEditAssetPath.md"
DEFAULT_ITEMS_INPUT = str(_TOOLS_DIR / "Entity_list.json")
DEFAULT_OUTPUT     = str(_TOOLS_DIR / "AssetIndex.json")
DEFAULT_UNSPAWNABLE = r"C:\RustModding\Carbon Server\server\carbon\data\Sandbox\UnspawnablePrefabs.json"
DEFAULT_ICON_BASE  = "https://raw.githubusercontent.com/trentbredahl-sudo/Rust_Sandbox_Mod/master/Icons/"
DEFAULT_ICON_LAYOUT = "flat"   # "flat" => Icons/<filename>   ;   "nested" => Icons/<assets/.../filename>

LOCAL_PREFIX = "C:/RustModding/IconStorage/PreviewImages/"

# ---------------------------------------------------------------------------
# Category rule table   (first match wins, top-to-bottom)
# Path-segment substring  ->  category label shown in the UI
# Edit freely and re-run; the histogram printed at the end tells you the impact.
# ---------------------------------------------------------------------------

CATEGORY_RULES: list[tuple[str, str]] = [
    # NPCs / vehicles - Trains MUST come before Vehicle (first match wins)
    ("prefabs/npc/",                              "NPC"),
    ("content/vehicles/trains/",                  "Trains"),
    ("content/vehicles/",                         "Vehicle"),

    # Building - Building Core split out from generic Building
    ("prefabs/building core/",                    "Building Core"),
    ("prefabs/building boat/",                    "Building"),
    ("prefabs/building/",                         "Building"),
    ("prefabs/boat/",                             "Building"),
    ("content/building/",                         "Building"),

    ("prefabs/deployable/",                       "Deployable"),

    # Weapons / ammo / tools
    ("prefabs/weapons/",                          "Weapons"),
    ("prefabs/weapon mods/",                      "Weapons"),
    ("prefabs/ammo/",                             "Ammo"),
    ("prefabs/tools/",                            "Tools"),

    # Survival
    ("prefabs/food/",                             "Food"),
    ("prefabs/resource/",                         "Resource"),
    ("prefabs/plants/",                           "Plants"),
    ("prefabs/clothes/",                          "Clothing"),

    # Tech / decor
    ("prefabs/io/",                               "IO"),
    ("prefabs/componentitems/",                   "Components"),
    ("prefabs/wallpaper/",                        "Wallpaper"),
    ("prefabs/instruments/",                      "Instruments"),

    ("prefabs/missions/",                         "Missions"),

    # ---- Misc splits (these MUST come before the generic prefabs/misc/ catchall) ----
    ("prefabs/misc/xmas/",                        "Holiday & Event"),
    ("prefabs/misc/halloween/",                   "Holiday & Event"),
    ("prefabs/misc/easter/",                      "Holiday & Event"),
    ("prefabs/misc/chinesenewyear/",              "Holiday & Event"),
    ("prefabs/misc/summer_dlc/",                  "Holiday & Event"),
    ("prefabs/misc/decor_dlc/",                   "Holiday & Event"),
    ("prefabs/misc/artist_dlc/",                  "Holiday & Event"),
    ("prefabs/misc/twitch/",                      "Holiday & Event"),
    ("prefabs/misc/birthday_balloons_2025/",      "Holiday & Event"),
    ("prefabs/misc/poker_chips/",                 "Holiday & Event"),

    ("prefabs/misc/underwaterlabsdwelling/",      "Dwelling Decor"),
    ("prefabs/misc/desertbasedwelling/",          "Dwelling Decor"),
    ("prefabs/misc/deepseadwellings/",            "Dwelling Decor"),
    ("prefabs/misc/tunneldwelling/",              "Dwelling Decor"),
    ("prefabs/misc/divesite/",                    "Dwelling Decor"),

    # Generic Misc catchall (remaining prefabs/misc/* + small leftover folders)
    ("prefabs/misc/",                             "Misc"),
    ("prefabs/locks/",                            "Misc"),
    ("prefabs/voiceaudio/",                       "Misc"),
    ("prefabs/debris/",                           "Misc"),
    ("prefabs/physicstesting/",                   "Misc"),
    ("content/mesh decals/",                      "Misc"),

    # Monument-scale buckets
    ("bundled/prefabs/",                          "Monuments"),
    ("content/structures/",                       "Structures"),
    ("content/props/",                            "Props"),
    ("content/nature/",                           "Nature"),
]
DEFAULT_CATEGORY = "Unknown"

# ---------------------------------------------------------------------------
# Item category translation (Entity_list.json category -> final UI category)
# Hybrid: translate obvious overlaps onto existing prefab categories,
# keep niche item-only categories as their own tabs.
# ---------------------------------------------------------------------------

ITEM_CATEGORY_TRANSLATION: dict[str, str] = {
    # Direct overlaps -> map onto existing prefab categories
    "Clothing":                       "Clothing",
    "Weapons":                        "Weapons",
    "Components":                     "Components",
    "Ammo":                           "Ammo",
    "Food":                           "Food",
    "Resource":                       "Resource",
    "Tools":                          "Tools",
    "Building Blocks":                "Building",
    "Building - Doors & Hatches":     "Building",
    "Building - Wallpaper":           "Wallpaper",
    "Deployables (Other)":            "Deployable",
    "Electrical & IO":                "IO",
    "Environment & Terrain":          "Nature",
    "Flora & Plants":                 "Plants",
    "Monuments & Points of Interest": "Monuments",
    "NPCs & Animals":                 "NPC",
    "Ores & Mining":                  "Resource",
    "Vehicles & Transport":           "Vehicle",
    "World - Misc":                   "Misc",

    # Item-only categories kept as their own tabs
    "Items":                          "Items (Misc)",
    "Furniture & Storage":            "Furniture",
    "Loot Containers":                "Loot Containers",
    "Signs & Canvases":               "Signs",
    "Collectables":                   "Collectables",

    # Folded into Misc (too small / leftover bucketing)
    "Admin & Developer Tools":        "Misc",
    "Cinematic & Cameras":            "Misc",
    "Auto_Matched_Step1":             "Misc",
    "Auto_Matched_Step2":             "Misc",
}

# ---------------------------------------------------------------------------

QUOTED_LINE = re.compile(r'^\s*"(.+)"\s*$')


def normalize(path: str) -> str:
    """Backslashes -> forward slashes."""
    return path.replace("\\", "/")


def strip_local_prefix(path: str) -> str | None:
    """Drop the local PreviewImages prefix (case-insensitive). Return None if it doesn't match."""
    lower = path.lower()
    prefix_lower = LOCAL_PREFIX.lower()
    if not lower.startswith(prefix_lower):
        return None
    return path[len(LOCAL_PREFIX):]


def categorize(relative_path: str) -> str:
    lower = relative_path.lower()
    for needle, label in CATEGORY_RULES:
        if needle in lower:
            return label
    return DEFAULT_CATEGORY


def build_icon_url(base: str, layout: str, relative_png_path: str, filename: str) -> str:
    """relative_png_path includes 'assets/.../foo.prefab.png' (no leading slash)."""
    if not base.endswith("/"):
        base = base + "/"
    if layout == "flat":
        return base + urllib.parse.quote(filename)
    if layout == "nested":
        # Encode each segment so spaces in folder names become %20
        encoded = "/".join(urllib.parse.quote(seg) for seg in relative_png_path.split("/"))
        return base + encoded
    raise ValueError(f"Unknown icon-layout: {layout!r}")


def parse_line(raw: str) -> str | None:
    """Strip surrounding quotes and whitespace. Return None for blanks/junk."""
    s = raw.strip()
    if not s:
        return None
    m = QUOTED_LINE.match(s)
    return m.group(1) if m else s  # tolerate unquoted lines too


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",  default=DEFAULT_INPUT,  help="Path to RustEditAssetPath.md")
    parser.add_argument("--items",  default=DEFAULT_ITEMS_INPUT, help="Path to Entity_list.json (item icons + categories)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Where to write AssetIndex.json")
    parser.add_argument("--icon-base",   default=DEFAULT_ICON_BASE,   help="GitHub raw URL prefix for prefab icons")
    parser.add_argument("--icon-layout", default=DEFAULT_ICON_LAYOUT, choices=["flat", "nested"],
                        help="'flat': Icons/<filename>.   'nested': Icons/<full assets path>")
    parser.add_argument("--no-items", action="store_true", help="Skip Pass 2 (item merge)")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)

    if not input_path.is_file():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Counters for the report
    total_lines = 0
    skipped_blank = 0
    skipped_bad_prefix = 0
    skipped_not_prefab = 0
    duplicate_paths = 0

    seen_prefab_paths: set[str] = set()
    filename_to_paths: dict[str, list[str]] = defaultdict(list)
    category_counts: Counter[str] = Counter()
    unknown_samples: list[str] = []

    entries: list[dict] = []

    with input_path.open("r", encoding="utf-8") as f:
        for raw in f:
            total_lines += 1

            cleaned = parse_line(raw)
            if cleaned is None:
                skipped_blank += 1
                continue

            normalized = normalize(cleaned)
            relative_png = strip_local_prefix(normalized)
            if relative_png is None:
                skipped_bad_prefix += 1
                continue

            if not relative_png.lower().endswith(".prefab.png"):
                skipped_not_prefab += 1
                continue

            prefab_path = relative_png[:-len(".png")]   # drop only the .png
            filename = relative_png.rsplit("/", 1)[-1]
            short_name = filename.split(".", 1)[0]
            category = categorize(relative_png)

            if category == DEFAULT_CATEGORY and len(unknown_samples) < 10:
                unknown_samples.append(relative_png)

            if prefab_path in seen_prefab_paths:
                duplicate_paths += 1
                continue
            seen_prefab_paths.add(prefab_path)

            filename_to_paths[filename].append(prefab_path)
            category_counts[category] += 1

            entries.append({
                "ShortName":  short_name,
                "PrefabPath": prefab_path,
                "Category":   category,
                "IconUrl":    build_icon_url(args.icon_base, args.icon_layout, relative_png, filename),
                "IsItem":     False,
            })

    # ----- Pass 2: merge inventory items from Entity_list.json -----
    items_added = 0
    items_skipped_dup = 0
    items_unmapped_categories: Counter[str] = Counter()
    item_category_counts: Counter[str] = Counter()
    items_path = Path(args.items)

    if not args.no_items:
        if not items_path.is_file():
            print(f"WARNING: items file not found, skipping Pass 2: {items_path}", file=sys.stderr)
        else:
            seen_item_keys: set[tuple[str, str]] = set()  # (category, shortname) dedup within items
            with items_path.open("r", encoding="utf-8") as f:
                items_data = json.load(f)

            for raw_category, items_in_cat in items_data.items():
                if not isinstance(items_in_cat, dict):
                    continue
                final_category = ITEM_CATEGORY_TRANSLATION.get(raw_category)
                if final_category is None:
                    items_unmapped_categories[raw_category] += len(items_in_cat)
                    final_category = "Misc"   # safe fallback so nothing is lost
                for shortname, icon_url in items_in_cat.items():
                    key = (final_category, shortname)
                    if key in seen_item_keys:
                        items_skipped_dup += 1
                        continue
                    seen_item_keys.add(key)
                    entries.append({
                        "ShortName":  shortname,
                        "PrefabPath": shortname,    # items: shortname doubles as the sb.spawn lookup key
                        "Category":   final_category,
                        "IconUrl":    icon_url,
                        "IsItem":     True,
                    })
                    item_category_counts[final_category] += 1
                    items_added += 1

    # ----- Pass 3: apply proposed_icon_fixes.json (third icon-source layer) -----
    # Generated by tools/backfill_missing_icons.py. Only fills entries whose
    # IconUrl is empty after Passes 1 & 2. Original sources are never overwritten.
    fixes_applied = 0
    fixes_path = _TOOLS_DIR / "proposed_icon_fixes.json"
    if fixes_path.is_file():
        proposed = json.load(fixes_path.open(encoding="utf-8"))
        if isinstance(proposed, dict):
            for e in entries:
                if not (e.get("IconUrl") or "").strip():
                    url = proposed.get(e["ShortName"])
                    if url:
                        e["IconUrl"] = url
                        fixes_applied += 1

    # ----- Pass 3.5: normalize legacy Icon URL patterns -----
    # Some entries (originating from Entity_list.json and inherited via Pass-1
    # Jaccard hits) use the legacy long-form URL. Rewrite them to the canonical
    # short form so all icons resolve through one consistent path.
    ICON_URL_NORMALIZATIONS = [
        ("/refs/heads/master/Rust/server/carbon/plugins/Icons/", "/master/Icons/"),
    ]
    urls_normalized = 0
    for e in entries:
        url = e.get("IconUrl") or ""
        if not url:
            continue
        new_url = url
        for old, new in ICON_URL_NORMALIZATIONS:
            new_url = new_url.replace(old, new)
        if new_url != url:
            e["IconUrl"] = new_url
            urls_normalized += 1

    # ----- Pass 4: prune unspawnable prefabs -----
    # Plugin's ValidatePrefabs() dumps paths that GameManager.server.FindPrefab
    # rejects (asset-scene-gated, deprecated, etc.). Drop them so the UI never
    # offers something that fails on spawn. Items (IsItem=True) are left alone
    # because their PrefabPath is a shortname, not a real prefab path.
    pruned_count = 0
    unspawnable_path = Path(DEFAULT_UNSPAWNABLE)
    if unspawnable_path.is_file():
        with unspawnable_path.open("r", encoding="utf-8") as f:
            unspawnable_list = json.load(f)
        unspawnable_set = set(unspawnable_list) if isinstance(unspawnable_list, list) else set()

        before = len(entries)
        entries = [
            e for e in entries
            if e.get("IsItem") or e.get("PrefabPath") not in unspawnable_set
        ]
        pruned_count = before - len(entries)

    # Sort entries: category first, items-after-prefabs within category, then shortname
    entries.sort(key=lambda e: (e["Category"], e["IsItem"], e["ShortName"]))

    output_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    # Filename collisions are only relevant in flat layout
    collisions = {fn: paths for fn, paths in filename_to_paths.items() if len(paths) > 1}
    collisions_path = output_path.parent / "icon_filename_collisions.txt"
    if collisions and args.icon_layout == "flat":
        with collisions_path.open("w", encoding="utf-8") as f:
            f.write(f"# {len(collisions)} filename(s) appear at multiple prefab paths.\n")
            f.write("# In flat upload mode, these will overwrite each other on GitHub.\n\n")
            for fn, paths in sorted(collisions.items()):
                f.write(f"{fn}\n")
                for p in paths:
                    f.write(f"    {p}\n")
                f.write("\n")
    elif collisions_path.exists():
        collisions_path.unlink()

    # ----- Combined histogram across prefabs + items (final UI tabs) -----
    final_histogram: Counter[str] = Counter()
    prefab_per_cat: Counter[str] = Counter()
    item_per_cat: Counter[str] = Counter()
    for e in entries:
        final_histogram[e["Category"]] += 1
        if e["IsItem"]:
            item_per_cat[e["Category"]] += 1
        else:
            prefab_per_cat[e["Category"]] += 1

    # ----- Missing-icon audit: any entry whose IconUrl is empty/whitespace -----
    missing_by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if not (e.get("IconUrl") or "").strip():
            missing_by_cat[e["Category"]].append(e)

    missing_total = sum(len(v) for v in missing_by_cat.values())
    missing_path = output_path.parent / "missing_icons.txt"
    if missing_total > 0:
        with missing_path.open("w", encoding="utf-8") as f:
            f.write(f"# {missing_total} entr{'y' if missing_total == 1 else 'ies'} have an empty IconUrl.\n")
            f.write("# Grouped by category. Format: <ShortName>  [P=prefab, I=item]  <PrefabPath>\n\n")
            for cat in sorted(missing_by_cat.keys()):
                rows = missing_by_cat[cat]
                f.write(f"## {cat}  ({len(rows)})\n")
                for e in sorted(rows, key=lambda r: r["ShortName"].lower()):
                    tag = "I" if e["IsItem"] else "P"
                    f.write(f"  [{tag}]  {e['ShortName']:<40}  {e['PrefabPath']}\n")
                f.write("\n")
    elif missing_path.exists():
        missing_path.unlink()

    # ----- Report -----
    print("=" * 70)
    print(f"Prefab input : {input_path}")
    if not args.no_items:
        print(f"Items input  : {items_path}")
    print(f"Output       : {output_path}")
    print(f"Icon base    : {args.icon_base}")
    print(f"Icon layout  : {args.icon_layout}")
    print("-" * 70)
    print(f"Pass 1 (prefabs from RustEdit asset index)")
    print(f"  lines read         : {total_lines}")
    print(f"  blank/malformed    : {skipped_blank}")
    print(f"  wrong prefix       : {skipped_bad_prefix}")
    print(f"  not .prefab.png    : {skipped_not_prefab}")
    print(f"  duplicate paths    : {duplicate_paths}")
    print(f"  prefab records     : {sum(prefab_per_cat.values())}")
    print("-" * 70)
    print(f"Pass 2 (items from Entity_list.json){' [SKIPPED]' if args.no_items else ''}")
    print(f"  item records added : {items_added}")
    print(f"  duplicates skipped : {items_skipped_dup}")
    if items_unmapped_categories:
        print(f"  unmapped categories (folded into 'Misc'):")
        for cat, count in items_unmapped_categories.most_common():
            print(f"    {cat:<35} {count}")
    print("-" * 70)
    print(f"Pass 3 (proposed_icon_fixes.json overlay)")
    print(f"  icons backfilled   : {fixes_applied}")
    print(f"Pass 3.5 (legacy URL normalization)")
    print(f"  urls rewritten     : {urls_normalized}")
    print(f"Pass 4 (unspawnable prefab prune)")
    if unspawnable_path.is_file():
        print(f"  source             : {unspawnable_path}")
        print(f"  entries removed    : {pruned_count}")
    else:
        print(f"  SKIPPED (not found): {unspawnable_path}")
    print("-" * 70)
    print(f"Total records in AssetIndex.json : {len(entries)}")
    print(f"Final tab count (categories)     : {len(final_histogram)}")
    print("-" * 70)
    print(f"{'Category':<22} {'Total':>7}  {'Prefabs':>9}  {'Items':>7}")
    for cat in sorted(final_histogram.keys(), key=lambda c: -final_histogram[c]):
        print(f"  {cat:<20} {final_histogram[cat]:>7}  {prefab_per_cat[cat]:>9}  {item_per_cat[cat]:>7}")
    print("-" * 70)
    if unknown_samples:
        print(f"Sample 'Unknown' prefab paths ({prefab_per_cat['Unknown']} total):")
        for s in unknown_samples:
            print(f"  {s}")
        print("-> add a CATEGORY_RULES entry to fix.")
    if collisions and args.icon_layout == "flat":
        print(f"Flat-layout icon filename collisions: {len(collisions)}")
        print(f"-> see {collisions_path}")
    if missing_total > 0:
        print(f"Entries with empty IconUrl: {missing_total}")
        print(f"-> see {missing_path}")
        # Per-category breakdown so we can see which tabs are most affected
        print(f"   {'Category':<22} {'Missing':>8}")
        for cat in sorted(missing_by_cat.keys(), key=lambda c: -len(missing_by_cat[c])):
            print(f"   {cat:<22} {len(missing_by_cat[cat]):>8}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
