#!/usr/bin/env python3
"""Material-state fingerprinting and safe compaction for the cloud runner.

The main synchronizer intentionally records attempts, success timestamps, and
empty runs.  Those fields are useful at runtime but would make a SQLite file
change on every 15-minute poll.  This helper fingerprints only durable research
content, source mappings, version snapshots, and notification events.  The
workflow commits the database to its state branch only when this fingerprint
changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Sequence


EMPTY_FINGERPRINT = "empty"


ITEM_SOURCE_QUERY = """
    SELECT source_id,external_id,item_id,source_url,raw_hash
    FROM item_sources ORDER BY source_id,external_id
"""

ITEM_VERSION_QUERY = """
    SELECT item_id,source_id,raw_hash,title,url,published_at,summary
    FROM item_versions ORDER BY item_id,source_id,raw_hash
"""


def material_queries(connection: sqlite3.Connection) -> Sequence[str]:
    """Build migration-safe material queries for the durable state branch."""

    item_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(items)")
    }
    event_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(run_events)")
    }
    original_date = (
        "original_published_at"
        if "original_published_at" in item_columns
        else "NULL AS original_published_at"
    )
    suppressed_at = (
        "suppressed_at" if "suppressed_at" in event_columns else "NULL AS suppressed_at"
    )
    suppression_reason = (
        "suppression_reason"
        if "suppression_reason" in event_columns
        else "NULL AS suppression_reason"
    )
    return (
        f"""
        SELECT canonical_key,item_type,title,normalized_title,url,doi,authors,venue,
               published_at,{original_date},summary,topics_json,baseline
        FROM items ORDER BY id
        """,
        ITEM_SOURCE_QUERY,
        ITEM_VERSION_QUERY,
        f"""
        SELECT run_id,item_id,source_id,event_type,created_at,
               {suppressed_at},{suppression_reason}
        FROM run_events ORDER BY run_id,item_id,event_type
        """,
    )


BRIEF_QUERY = """
    SELECT item_id,public_id,source_hash,
           CASE WHEN status='professional' THEN 'professional' ELSE 'pending' END AS publication_status,
           model,brief_json,generated_at,attempt_count,validation_hash
    FROM item_briefs ORDER BY item_id
"""


def brief_material_query(connection: sqlite3.Connection) -> str:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(item_briefs)")
    }
    required = {
        "item_id", "public_id", "source_hash", "status", "model", "brief_json",
        "generated_at", "attempt_count", "validation_hash",
    }
    if required.issubset(columns):
        return BRIEF_QUERY
    # Forward-compatible read of an older state snapshot before radar.connect
    # applies its schema migration. Missing fields deliberately alter the next
    # post-migration fingerprint, ensuring the upgraded state is persisted.
    expression = lambda name, fallback: name if name in columns else f"{fallback} AS {name}"
    return "SELECT " + ",".join(
        [
            expression("item_id", "0"),
            expression("public_id", "''"),
            expression("source_hash", "''"),
            "CASE WHEN status='professional' THEN 'professional' ELSE 'pending' END AS publication_status"
            if "status" in columns else "'pending' AS publication_status",
            expression("model", "NULL"),
            expression("brief_json", "'{}'"),
            expression("generated_at", "NULL"),
            expression("attempt_count", "0"),
            expression("validation_hash", "NULL"),
        ]
    ) + " FROM item_briefs ORDER BY item_id"


def connect_readonly(path: Path) -> sqlite3.Connection:
    # ``mode=ro`` is attractive but some macOS SQLite builds cannot open a
    # freshly copied WAL-mode database through a URI until sidecar discovery
    # has happened once. ``query_only`` preserves the same no-write contract
    # while working reliably for local verification and Ubuntu runners.
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def required_tables(connection: sqlite3.Connection) -> bool:
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {"items", "item_sources", "item_versions", "run_events"}.issubset(names)


def encoded_rows(rows: Iterable[sqlite3.Row]) -> Iterable[bytes]:
    for row in rows:
        values = [row[key] for key in row.keys()]
        yield json.dumps(
            values, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")


def material_fingerprint(path: Path) -> str:
    if not path.is_file():
        return EMPTY_FINGERPRINT
    connection = connect_readonly(path)
    try:
        if not required_tables(connection):
            return EMPTY_FINGERPRINT
        item_count = int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        version_count = int(
            connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0]
        )
        event_count = int(
            connection.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        )
        if item_count == 0 and version_count == 0 and event_count == 0:
            return EMPTY_FINGERPRINT
        digest = hashlib.sha256()
        queries = list(material_queries(connection))
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "item_briefs" in tables:
            queries.append(brief_material_query(connection))
        for query in queries:
            digest.update(b"\x1equery\x1f")
            for encoded in encoded_rows(connection.execute(query)):
                digest.update(encoded)
                digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def verify(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = connect_readonly(path)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {result}")
        if not required_tables(connection):
            raise RuntimeError("SQLite database does not contain the radar schema")
    finally:
        connection.close()


def compact(path: Path) -> None:
    """Prune empty run bookkeeping and produce a self-contained DB file."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        # Keep every run referenced by an event plus the latest run, which is
        # useful for diagnostics.  Empty polling runs carry no durable state.
        connection.execute(
            """
            DELETE FROM runs
            WHERE id NOT IN (SELECT DISTINCT run_id FROM run_events)
              AND id <> (SELECT MAX(id) FROM runs)
            """
        )
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {result}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    for command in ("fingerprint", "verify", "compact"):
        child = subparsers.add_parser(command)
        child.add_argument("database", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "fingerprint":
            print(material_fingerprint(args.database))
        elif args.command == "verify":
            verify(args.database)
            print("OK")
        elif args.command == "compact":
            compact(args.database)
            verify(args.database)
            print("OK")
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
