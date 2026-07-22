import datetime as dt
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import briefs
import item_page
import radar
from cloud import publish_site, state_db


class CloudPublishingTests(unittest.TestCase):
    HISTORY_CHECKED_AT = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    HISTORY_CUTOFF = radar.retention.history_cutoff(
        {"history_window_years": 5}, today=HISTORY_CHECKED_AT.date()
    )

    def database(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(radar.SCHEMA)
        briefs.ensure_schema(connection)
        connection.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) "
            "VALUES('s','Source','rss','paper','https://source.example','https://source.example/feed','{}',1)"
        )
        run_id = connection.execute(
            "INSERT INTO runs(started_at,status) VALUES('2026-07-19T00:00:00Z','ok')"
        ).lastrowid
        for number, baseline in ((1, 1), (2, 0)):
            item_id = connection.execute(
                """
                INSERT INTO items(
                    canonical_key,item_type,title,normalized_title,url,published_at,
                    original_published_at,summary,topics_json,discovered_at,updated_at,baseline
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"item:{number}", "paper", f"Paper {number}", f"paper {number}",
                    f"https://source.example/{number}", "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "A source abstract that must not become the feed link.", "[]",
                    "2026-07-19T00:00:00Z", "2026-07-19T00:00:00Z", baseline,
                ),
            ).lastrowid
            connection.execute(
                "INSERT INTO item_sources(source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at) "
                "VALUES('s',?,?,?,?,?,?)",
                (str(number), item_id, f"https://source.example/{number}", str(number), "now", "now"),
            )
            connection.execute(
                "INSERT INTO item_versions(item_id,source_id,raw_hash,captured_at,title,url,published_at,summary) "
                "VALUES(?,'s',?,'now',?,?,?,?)",
                (
                    item_id, str(number), f"Paper {number}", f"https://source.example/{number}",
                    "2026-01-01T00:00:00Z", "A source abstract",
                ),
            )
            if not baseline:
                connection.execute(
                    "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) "
                    "VALUES(?,?,'s','new','2026-07-19T01:00:00Z')",
                    (run_id, item_id),
                )
        connection.commit()
        briefs.ensure_fallback_briefs(connection)
        rows = connection.execute(
            "SELECT item_id,source_hash,brief_json FROM item_briefs ORDER BY item_id"
        ).fetchall()
        model = "test"
        for row in rows:
            professional = json.loads(row["brief_json"])
            professional["title_zh"] = f"中文：Paper {row['item_id']}"
            professional["evidence_level"] = "source_summary"
            professional["supporting_quote"] = (
                "A source abstract that must not become the feed link."
            )
            validation_hash = briefs.professional_validation_hash(
                row["source_hash"], model, professional
            )
            connection.execute(
                "UPDATE item_briefs SET status='professional',model=?,brief_json=?,"
                "validation_hash=? WHERE item_id=?",
                (
                    model,
                    json.dumps(professional, ensure_ascii=False),
                    validation_hash,
                    row["item_id"],
                ),
            )
        connection.commit()
        return connection

    def test_full_feed_links_to_local_briefs_and_keeps_original_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            site = Path(directory) / "site"
            item_page.export_item_pages(
                connection, site, "https://reader.example/radar/"
            )
            connection.close()
            destination = site / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            root = ET.parse(destination).getroot()
            items = root.findall("channel/item")
            self.assertEqual(count, 2)
            self.assertEqual(len(items), 2)
            description = root.findtext("channel/description") or ""
            self.assertIn("滚动最近 5 年", description)
            self.assertIn("旧资料今天发生的实质更新仍可发布", description)
            self.assertIn("迟发现的窗口外旧资料不作为新增", description)
            self.assertIn(self.HISTORY_CUTOFF, description)
            for item in items:
                link = item.findtext("link") or ""
                description = item.findtext("description") or ""
                self.assertTrue(link.startswith("https://reader.example/radar/item.html?id="))
                self.assertNotEqual(link, "https://source.example/1")
                self.assertIn("【核心思想】", description)
                self.assertIn("【核心思想原文依据】", description)
                self.assertIn("【原文入口】", description)
                public_id = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(link).query
                )["id"][0]
                self.assertTrue(
                    (site / "items" / public_id[:2] / f"{public_id}.json").is_file()
                )

    def test_unprofessional_event_is_withheld_from_full_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.execute(
                "UPDATE item_briefs SET status='retry',model=NULL WHERE item_id=(SELECT id FROM items WHERE baseline=0)"
            )
            connection.commit()
            connection.close()
            destination = Path(directory) / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            self.assertEqual(count, 1)
            titles = [node.findtext("title") for node in ET.parse(destination).findall("channel/item")]
            self.assertEqual(titles, ["中文：Paper 1"])

    def test_unprofessional_untouched_baseline_is_withheld_from_full_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.execute(
                "UPDATE item_briefs SET status='retry',model=NULL "
                "WHERE item_id=(SELECT id FROM items WHERE baseline=1)"
            )
            connection.commit()
            connection.close()
            destination = Path(directory) / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            self.assertEqual(count, 1)
            titles = [
                node.findtext("title")
                for node in ET.parse(destination).findall("channel/item")
            ]
            self.assertEqual(titles, ["中文：Paper 2"])

    def test_full_feed_uses_immutable_date_for_history_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.execute(
                "UPDATE items SET original_published_at='2020-01-01T00:00:00Z',"
                "published_at='2026-07-19T00:00:00Z' WHERE baseline=1"
            )
            connection.commit()
            connection.close()

            destination = Path(directory) / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            self.assertEqual(count, 1)
            titles = [
                node.findtext("title")
                for node in ET.parse(destination).findall("channel/item")
            ]
            self.assertEqual(titles, ["中文：Paper 2"])

    def test_current_update_to_old_item_remains_in_full_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.execute(
                "UPDATE items SET original_published_at='2020-01-01T00:00:00Z' "
                "WHERE baseline=0"
            )
            connection.execute(
                "UPDATE run_events SET event_type='updated' WHERE event_type='new'"
            )
            connection.commit()
            connection.close()

            destination = Path(directory) / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            self.assertEqual(count, 2)
            titles = [
                node.findtext("title")
                for node in ET.parse(destination).findall("channel/item")
            ]
            self.assertIn("[更新] 中文：Paper 2", titles)

    def test_late_discovered_old_item_is_not_published_as_new(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.execute(
                "UPDATE items SET original_published_at='2020-01-01T00:00:00Z' "
                "WHERE baseline=0"
            )
            connection.commit()
            connection.close()

            destination = Path(directory) / "full.xml"
            count = publish_site.build_full_feed(
                database,
                destination,
                "https://reader.example/radar/",
                history_cutoff=self.HISTORY_CUTOFF,
            )
            self.assertEqual(count, 1)
            titles = [
                node.findtext("title")
                for node in ET.parse(destination).findall("channel/item")
            ]
            self.assertEqual(titles, ["中文：Paper 1"])

    def test_suppressed_or_expired_events_are_not_in_full_feed(self):
        for update_sql in (
            "UPDATE run_events SET suppressed_at='2026-07-19T02:00:00Z'",
            "UPDATE run_events SET created_at='2020-07-19T01:00:00Z'",
        ):
            with self.subTest(update_sql=update_sql), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "radar.sqlite3"
                connection = self.database(database)
                connection.execute(update_sql)
                connection.commit()
                connection.close()

                destination = Path(directory) / "full.xml"
                count = publish_site.build_full_feed(
                    database,
                    destination,
                    "https://reader.example/radar/",
                    history_cutoff=self.HISTORY_CUTOFF,
                )
                self.assertEqual(count, 1)
                titles = [
                    node.findtext("title")
                    for node in ET.parse(destination).findall("channel/item")
                ]
                self.assertEqual(titles, ["中文：Paper 1"])

    def test_brief_content_is_material_but_retry_bookkeeping_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.close()
            before = state_db.material_fingerprint(database)

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE item_briefs SET last_attempt_at='later',last_error='temporary'"
            )
            connection.commit()
            connection.close()
            self.assertEqual(state_db.material_fingerprint(database), before)

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE item_briefs SET status='retry' WHERE status='fallback'"
            )
            connection.commit()
            connection.close()
            self.assertEqual(state_db.material_fingerprint(database), before)

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT item_id,brief_json FROM item_briefs ORDER BY item_id LIMIT 1"
            ).fetchone()
            content = json.loads(row[1])
            content["one_liner"] = "A materially different professional conclusion."
            connection.execute(
                "UPDATE item_briefs SET brief_json=? WHERE item_id=?",
                (json.dumps(content, ensure_ascii=False), row[0]),
            )
            connection.commit()
            connection.close()
            self.assertNotEqual(state_db.material_fingerprint(database), before)

    def test_retention_date_and_event_suppression_are_material_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "radar.sqlite3"
            connection = self.database(database)
            connection.close()
            before = state_db.material_fingerprint(database)

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE items SET original_published_at='2025-12-31T00:00:00Z' "
                "WHERE id=(SELECT MIN(id) FROM items)"
            )
            connection.commit()
            connection.close()
            changed_date = state_db.material_fingerprint(database)
            self.assertNotEqual(changed_date, before)

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE items SET original_published_at='2026-01-01T00:00:00Z' "
                "WHERE id=(SELECT MIN(id) FROM items)"
            )
            connection.execute(
                "UPDATE run_events SET suppressed_at='2026-07-19T02:00:00Z',"
                "suppression_reason='outside_retention_window'"
            )
            connection.commit()
            connection.close()
            self.assertNotEqual(state_db.material_fingerprint(database), before)

    def test_material_fingerprint_reads_pre_retention_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE items(
                    id INTEGER PRIMARY KEY,canonical_key TEXT,item_type TEXT,title TEXT,
                    normalized_title TEXT,url TEXT,doi TEXT,authors TEXT,venue TEXT,
                    published_at TEXT,summary TEXT,topics_json TEXT,baseline INTEGER
                );
                CREATE TABLE item_sources(
                    source_id TEXT,external_id TEXT,item_id INTEGER,source_url TEXT,raw_hash TEXT
                );
                CREATE TABLE item_versions(
                    item_id INTEGER,source_id TEXT,raw_hash TEXT,title TEXT,url TEXT,
                    published_at TEXT,summary TEXT
                );
                CREATE TABLE run_events(
                    run_id INTEGER,item_id INTEGER,source_id TEXT,event_type TEXT,created_at TEXT
                );
                INSERT INTO items VALUES(
                    1,'old','paper','Old','old','https://example.test/old',NULL,NULL,NULL,
                    '2020-01-01T00:00:00Z','Summary','[]',1
                );
                INSERT INTO item_sources VALUES('s','old',1,'https://example.test/old','hash');
                INSERT INTO item_versions VALUES(
                    1,'s','hash','Old','https://example.test/old','2020-01-01T00:00:00Z','Summary'
                );
                INSERT INTO run_events VALUES(1,1,'s','new','2020-01-02T00:00:00Z');
                """
            )
            connection.commit()
            connection.close()

            fingerprint = state_db.material_fingerprint(database)
            self.assertRegex(fingerprint, r"^[a-f0-9]{64}$")

    def test_material_fingerprint_reads_pre_attestation_brief_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(radar.SCHEMA)
            connection.execute(
                """
                INSERT INTO items(canonical_key,item_type,title,normalized_title,topics_json,
                                  discovered_at,updated_at,baseline)
                VALUES('old','paper','Old','old','[]','now','now',1)
                """
            )
            connection.execute(
                """
                CREATE TABLE item_briefs(
                    item_id INTEGER PRIMARY KEY,public_id TEXT,source_hash TEXT,status TEXT,
                    model TEXT,brief_json TEXT,generated_at TEXT,attempt_count INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO item_briefs VALUES(1,'public','source','fallback',NULL,'{}','now',0)"
            )
            connection.commit()
            connection.close()
            fingerprint = state_db.material_fingerprint(database)
            self.assertRegex(fingerprint, r"^[a-f0-9]{64}$")

    def test_public_status_separates_source_health_from_brief_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "latest.json"
            destination = root / "status.json"
            report.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "source_evaluated_count": 3,
                        "source_attempted_count": 2,
                        "source_success_count": 2,
                        "source_skipped_count": 1,
                        "source_failure_count": 0,
                        "source_checks": [
                            {"id": "fast", "name": "FAST", "status": "success"},
                            {"id": "nvmw", "name": "NVMW", "status": "success"},
                            {"id": "openalex", "name": "OpenAlex", "status": "fresh_cached"},
                        ],
                        "source_failures": [],
                        "brief_generation_ok": False,
                        "brief_generation_failure_count": 13,
                        "brief_failures": [
                            {
                                "id": "brief_generation",
                                "name": "专业简报生成",
                                "failed_count": 13,
                                "error": "证据校验失败；下次自动重试",
                            }
                        ],
                        "run_detected_new_count": 4,
                        "run_detected_updated_count": 2,
                        "history_window_years": 5,
                        "history_cutoff": self.HISTORY_CUTOFF,
                        "history_reference_date": self.HISTORY_CHECKED_AT.date().isoformat(),
                        "history_start_date": "2000-01-01",
                        "checked_at": self.HISTORY_CHECKED_AT.isoformat(),
                        "failures": [
                            {
                                "id": "brief_generation",
                                "name": "专业简报生成",
                                "failed_count": 13,
                                "error": "证据校验失败；下次自动重试",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            publish_site.build_status(report, destination)
            status = json.loads(destination.read_text(encoding="utf-8"))
            self.assertTrue(status["ok"])
            self.assertEqual(status["source_evaluated_count"], 3)
            self.assertEqual(status["source_attempted_count"], 2)
            self.assertEqual(status["source_success_count"], 2)
            self.assertEqual(status["source_skipped_count"], 1)
            self.assertEqual(status["source_failure_count"], 0)
            self.assertEqual(len(status["source_checks"]), 3)
            self.assertEqual(status["source_failures"], [])
            self.assertFalse(status["brief_generation_ok"])
            self.assertEqual(status["brief_generation_failure_count"], 13)
            self.assertEqual(len(status["brief_failures"]), 1)
            self.assertEqual(status["run_detected_new_count"], 4)
            self.assertEqual(status["run_detected_updated_count"], 2)
            self.assertEqual(status["history_window_years"], 5)
            self.assertEqual(status["history_cutoff"], self.HISTORY_CUTOFF)
            self.assertEqual(
                status["history_reference_date"],
                self.HISTORY_CHECKED_AT.date().isoformat(),
            )
            self.assertEqual(status["failures"][0]["failed_count"], 13)

    def test_publication_rejects_stale_or_overwide_history_policy(self):
        stale_checked_at = self.HISTORY_CHECKED_AT - dt.timedelta(days=3)
        with self.assertRaisesRegex(ValueError, "stale"):
            publish_site.report_history_policy(
                {
                    "history_window_years": 5,
                    "history_cutoff": radar.retention.history_cutoff(
                        {"history_window_years": 5},
                        today=stale_checked_at.date(),
                    ),
                    "history_reference_date": stale_checked_at.date().isoformat(),
                    "checked_at": stale_checked_at.isoformat(),
                }
            )

        with self.assertRaisesRegex(ValueError, "does not match"):
            publish_site.report_history_policy(
                {
                    "history_window_years": 5,
                    "history_cutoff": "2000-01-01",
                    "history_reference_date": self.HISTORY_CHECKED_AT.date().isoformat(),
                    "checked_at": self.HISTORY_CHECKED_AT.isoformat(),
                }
            )

        future_cutoff = (self.HISTORY_CHECKED_AT.date() + dt.timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "does not match"):
            publish_site.report_history_policy(
                {
                    "history_window_years": 5,
                    "history_cutoff": future_cutoff,
                    "history_reference_date": self.HISTORY_CHECKED_AT.date().isoformat(),
                    "checked_at": self.HISTORY_CHECKED_AT.isoformat(),
                }
            )

    def test_cross_midnight_report_uses_stable_history_reference_date(self):
        reference_date = self.HISTORY_CHECKED_AT.date() - dt.timedelta(days=1)
        checked_at = dt.datetime.combine(
            reference_date + dt.timedelta(days=1),
            dt.time.min,
            tzinfo=dt.timezone.utc,
        )
        cutoff = radar.retention.history_cutoff(
            {"history_window_years": 5}, today=reference_date
        )
        self.assertEqual(
            publish_site.report_history_policy(
                {
                    "history_window_years": 5,
                    "history_cutoff": cutoff,
                    "history_reference_date": reference_date.isoformat(),
                    "checked_at": checked_at.isoformat(),
                }
            ),
            (cutoff, 5),
        )

    def test_publication_fails_closed_without_report_history_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "latest.json"
            destination = root / "status.json"
            report.write_text(json.dumps({"ok": True}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "history_cutoff"):
                publish_site.build_status(report, destination)
            self.assertFalse(destination.exists())

    def test_publish_entrypoint_validates_cutoff_before_mutating_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "radar.sqlite3"
            connection = self.database(database)
            connection.close()
            report = root / "latest.json"
            report.write_text(json.dumps({"ok": True}), encoding="utf-8")
            site = root / "site"
            site.mkdir()
            live = site / "live.xml"
            live.write_text("unchanged", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "history_cutoff"):
                publish_site.main(
                    [
                        "--site",
                        str(site),
                        "--database",
                        str(database),
                        "--report",
                        str(report),
                        "--base-url",
                        "https://reader.example/radar/",
                    ]
                )
            self.assertEqual(live.read_text(encoding="utf-8"), "unchanged")

    def test_live_feed_description_exposes_rolling_history_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "live.xml"
            feed.write_text(
                "<?xml version='1.0' encoding='utf-8'?><rss version='2.0'>"
                "<channel><title>Live</title><link>http://127.0.0.1/</link>"
                "<description>old</description></channel></rss>",
                encoding="utf-8",
            )
            publish_site.rewrite_live_feed(
                feed,
                "https://reader.example/radar/",
                history_window_years=5,
                history_cutoff=self.HISTORY_CUTOFF,
            )
            description = ET.parse(feed).findtext("channel/description") or ""
            self.assertIn("滚动最近 5 年", description)
            self.assertIn("旧资料今天发生的实质更新仍会发布", description)
            self.assertIn("迟发现的窗口外旧资料不作为新增", description)
            self.assertIn(self.HISTORY_CUTOFF, description)


if __name__ == "__main__":
    unittest.main()
