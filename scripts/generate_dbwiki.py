#!/usr/bin/env python3
"""Rebuild dbwiki.html from the connected database.

  python scripts/generate_dbwiki.py
  python scripts/generate_dbwiki.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.dbwiki import WIKI_PATH, generate_dbwiki, refresh_dbwiki  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Mark database wiki HTML from the live schema.")
    parser.add_argument("--force", action="store_true", help="Rewrite even if the schema fingerprint is unchanged.")
    args = parser.parse_args()
    if args.force:
        path = generate_dbwiki()
        print(f"Wrote {path}")
    else:
        path = refresh_dbwiki(force=False)
        if path:
            print(f"Wrote {path}")
        else:
            print(f"Unchanged {WIKI_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
