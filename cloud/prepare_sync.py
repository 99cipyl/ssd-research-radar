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


def prepare(config_path: Path, database: Path, mode: str) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for source in config["sources"]:
        source_id = source["id"]
        if mode == "frequent":
            source["enabled"] = source_id not in ACADEMIC_SOURCE_IDS
        elif mode in {"academic", "monthly"}:
            source["enabled"] = source_id in ACADEMIC_SOURCE_IDS
        elif mode == "manual":
            source["enabled"] = True
        else:
            raise ValueError(f"unknown sync mode: {mode}")
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # The material-only state branch deliberately does not commit source poll
    # timestamps. On normal daily academic runs, suppress radar.py's automatic
    # 30-day full OpenAlex scan; the separate monthly job explicitly uses
    # --full. This prevents a stale persisted timestamp from causing a full
    # academic rescan every day after day 30.
    if mode == "academic" and database.is_file():
        connection = sqlite3.connect(database)
        try:
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            connection.execute(
                "UPDATE sources SET last_full_at=? WHERE id='openalex_ssd' AND initialized=1",
                (now,),
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
