#!/usr/bin/env python3
"""Select a rate-appropriate source set for a scheduled cloud sync."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Sequence


ACADEMIC_SOURCE_IDS = {"fast_dblp", "openalex_ssd"}


def _utc_now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def prepare(config_path: Path, database: Path, mode: str) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for source in config["sources"]:
        source_id = source["id"]
        if mode == "frequent":
            source["enabled"] = source_id not in ACADEMIC_SOURCE_IDS
        elif mode == "academic":
            source["enabled"] = source_id in ACADEMIC_SOURCE_IDS
        elif mode == "monthly":
            # A 25-year OpenAlex keyword replay can exceed the documented free
            # daily search budget and lose all fetched pages on a late 429.
            # DBLP's bounded FAST TOC scan remains safe for a monthly full pass.
            source["enabled"] = source_id == "fast_dblp"
        elif mode == "manual":
            source["enabled"] = True
        else:
            raise ValueError(f"unknown sync mode: {mode}")
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # The material-only state branch deliberately does not commit source poll
    # timestamps. Suppress radar.py's automatic 30-day OpenAlex replay on both
    # daily academic and manual all-source runs. OpenAlex is covered by its
    # rolling one-year window after the initial baseline; the monthly --full
    # job is intentionally DBLP-only because a 25-year OpenAlex replay can
    # exceed the free search budget and discard all work on a late 429.
    if mode in {"academic", "manual"} and database.is_file():
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE sources SET last_full_at=? WHERE id='openalex_ssd' AND initialized=1",
                (_utc_now_iso(),),
            )
            connection.commit()
        finally:
            connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("sources.json"))
    parser.add_argument("--database", type=Path, default=Path("data/radar.sqlite3"))
    parser.add_argument(
        "--mode", choices=("frequent", "academic", "monthly", "manual"), required=True
    )
    args = parser.parse_args(argv)
    prepare(args.config, args.database, args.mode)
    print(f"Prepared {args.mode} source set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
