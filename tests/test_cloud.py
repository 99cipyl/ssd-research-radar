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
                    canonical_key,item_type,title,normalized_title,url,published_at,summary,
                    topics_json,discovered_at,updated_at,baseline
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"item:{number}", "paper", f"Paper {number}", f"paper {number}",
                    f"https://source.example/{number}", "2026-01-01T00:00:00Z",
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
        row = connection.execute(
            "SELECT item_id,brief_json FROM item_briefs WHERE item_id=(SELECT id FROM items WHERE baseline=0)"
        ).fetchone()
        professional = json.loads(row["brief_json"])
        professional["evidence_level"] = "source_summary"
        professional["supporting_quote"] = "A source abstract that must not become the feed link."
        model = "test"
        validation_hash = briefs.professional_validation_hash(
            connection.execute(
                "SELECT source_hash FROM item_briefs WHERE item_id=?", (row["item_id"],)
            ).fetchone()[0],
            model,
            professional,
        )
        connection.execute(
            "UPDATE item_briefs SET status='professional',model=?,brief_json=?,validation_hash=? WHERE item_id=?",
            (model, json.dumps(professional, ensure_ascii=False), validation_hash, row["item_id"]),
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
                database, destination, "https://reader.example/radar/"
            )
            root = ET.parse(destination).getroot()
            items = root.findall("channel/item")
            self.assertEqual(count, 2)
            self.assertEqual(len(items), 2)
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
                database, destination, "https://reader.example/radar/"
            )
            self.assertEqual(count, 1)
            titles = [node.findtext("title") for node in ET.parse(destination).findall("channel/item")]
            self.assertEqual(titles, ["Paper 1"])

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


if __name__ == "__main__":
    unittest.main()
