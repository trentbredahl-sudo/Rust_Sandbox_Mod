"""
Prune entries from AssetIndex.json whose PrefabPath appears in UnspawnablePrefabs.json.

- Matches by exact PrefabPath string.
- Skips items (IsItem == true) defensively, even though the dump only contains prefabs.
- Writes AssetIndex.json back in place (pretty-printed, UTF-8).
"""

import json
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ASSET_INDEX = TOOLS_DIR / "AssetIndex.json"
UNSPAWNABLE = TOOLS_DIR / "UnspawnablePrefabs.json"


def main() -> None:
    with ASSET_INDEX.open("r", encoding="utf-8") as f:
        entries = json.load(f)
    with UNSPAWNABLE.open("r", encoding="utf-8") as f:
        bad = json.load(f)

    bad_set = set(bad)
    before = len(entries)

    kept = [
        e for e in entries
        if e.get("IsItem") or e.get("PrefabPath") not in bad_set
    ]
    pruned = before - len(kept)

    with ASSET_INDEX.open("w", encoding="utf-8") as f:
        json.dump(kept, f, indent=2, ensure_ascii=False)

    print(f"Loaded {before} entries from AssetIndex.json")
    print(f"Loaded {len(bad_set)} unspawnable paths")
    print(f"Pruned {pruned} entries")
    print(f"Wrote {len(kept)} entries back to AssetIndex.json")


if __name__ == "__main__":
    main()
