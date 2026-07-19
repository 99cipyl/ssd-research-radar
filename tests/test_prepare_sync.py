import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cloud import prepare_sync


ROOT = Path(__file__).resolve().parents[1]


class PrepareSyncTests(unittest.TestCase):
    def write_config(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "sources": [
                        {"id": "fast_dblp", "enabled": False},
                        {"id": "openalex_ssd", "enabled": False},
                        {"id": "safari_eth", "enabled": False},
                        {"id": "future_nonacademic", "enabled": False},
                    ]
                }
            ),
            encoding="utf-8",
        )

    def enabled_sources(self, path: Path) -> set[str]:
        config = json.loads(path.read_text(encoding="utf-8"))
        return {source["id"] for source in config["sources"] if source["enabled"]}

    def write_database(
        self, path: Path, *, initialized: int = 1, last_full_at: str = "2020-01-01T00:00:00Z"
    ) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE sources(id TEXT PRIMARY KEY, initialized INTEGER, last_full_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO sources(id,initialized,last_full_at) VALUES(?,?,?)",
            [
                ("openalex_ssd", initialized, last_full_at),
                ("fast_dblp", 1, "2021-01-01T00:00:00Z"),
            ],
        )
        connection.commit()
        connection.close()

    def last_full_at(self, path: Path, source_id: str) -> str:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT last_full_at FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            return row[0]
        finally:
            connection.close()

    def test_each_mode_selects_its_explicit_source_set(self):
        expected = {
            "frequent": {"safari_eth", "future_nonacademic"},
            "academic": {"fast_dblp", "openalex_ssd"},
            "monthly": {"fast_dblp"},
            "manual": {
                "fast_dblp",
                "openalex_ssd",
                "safari_eth",
                "future_nonacademic",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode, enabled in expected.items():
                with self.subTest(mode=mode):
                    config = root / f"{mode}.json"
                    self.write_config(config)
                    prepare_sync.prepare(config, root / "missing.sqlite3", mode)
                    self.assertEqual(self.enabled_sources(config), enabled)

    def test_academic_and_manual_suppress_automatic_openalex_full_replay(self):
        frozen = "2026-07-20T04:05:06Z"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("academic", "manual"):
                with self.subTest(mode=mode):
                    config = root / f"{mode}.json"
                    database = root / f"{mode}.sqlite3"
                    self.write_config(config)
                    self.write_database(database)
                    with mock.patch.object(
                        prepare_sync, "_utc_now_iso", return_value=frozen
                    ):
                        prepare_sync.prepare(config, database, mode)
                    self.assertEqual(
                        self.last_full_at(database, "openalex_ssd"), frozen
                    )
                    self.assertEqual(
                        self.last_full_at(database, "fast_dblp"),
                        "2021-01-01T00:00:00Z",
                    )

    def test_uninitialized_openalex_baseline_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sources.json"
            database = root / "radar.sqlite3"
            self.write_config(config)
            self.write_database(database, initialized=0)
            prepare_sync.prepare(config, database, "academic")
            self.assertEqual(
                self.last_full_at(database, "openalex_ssd"),
                "2020-01-01T00:00:00Z",
            )

    def test_frequent_and_monthly_leave_full_scan_timestamps_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("frequent", "monthly"):
                with self.subTest(mode=mode):
                    config = root / f"{mode}.json"
                    database = root / f"{mode}.sqlite3"
                    self.write_config(config)
                    self.write_database(database)
                    prepare_sync.prepare(config, database, mode)
                    self.assertEqual(
                        self.last_full_at(database, "openalex_ssd"),
                        "2020-01-01T00:00:00Z",
                    )

    def test_missing_database_is_not_created_just_to_stamp_openalex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sources.json"
            database = root / "missing.sqlite3"
            self.write_config(config)
            prepare_sync.prepare(config, database, "manual")
            self.assertFalse(database.exists())

    def test_unknown_mode_fails_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "sources.json"
            self.write_config(config)
            before = config.read_bytes()
            with self.assertRaisesRegex(ValueError, "unknown sync mode"):
                prepare_sync.prepare(config, root / "missing.sqlite3", "surprise")
            self.assertEqual(config.read_bytes(), before)

    def test_monthly_workflow_disables_historical_model_backfill(self):
        workflow = (ROOT / ".github/workflows/publish-radar.yml").read_text(
            encoding="utf-8"
        )
        monthly_case = workflow.split("            monthly)", 1)[1].split(
            "              ;;", 1
        )[0]
        self.assertIn("export RADAR_BRIEF_HISTORY_LIMIT=0", monthly_case)


if __name__ == "__main__":
    unittest.main()
