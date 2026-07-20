import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import item_page
import briefs


BASE_SCHEMA = """
CREATE TABLE sources(
    id TEXT PRIMARY KEY,name TEXT,kind TEXT,category TEXT,homepage TEXT,endpoint TEXT,config_json TEXT
);
CREATE TABLE items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,canonical_key TEXT UNIQUE,item_type TEXT,title TEXT,
    normalized_title TEXT,url TEXT,doi TEXT,authors TEXT,venue TEXT,published_at TEXT,
    summary TEXT,topics_json TEXT,discovered_at TEXT,updated_at TEXT,baseline INTEGER
);
CREATE TABLE item_sources(
    source_id TEXT,external_id TEXT,item_id INTEGER,source_url TEXT,raw_hash TEXT,
    first_seen_at TEXT,last_seen_at TEXT
);
CREATE TABLE item_versions(
    item_id INTEGER,source_id TEXT,raw_hash TEXT,captured_at TEXT,title TEXT,url TEXT,
    published_at TEXT,summary TEXT
);
CREATE TABLE runs(id INTEGER PRIMARY KEY,started_at TEXT,status TEXT);
CREATE TABLE run_events(
    run_id INTEGER,item_id INTEGER,source_id TEXT,event_type TEXT,created_at TEXT,
    delivered_at TEXT,websub_published_at TEXT
);
"""


def database(with_briefs=True):
    connection = sqlite3.connect(":memory:")
    connection.executescript(BASE_SCHEMA)
    if with_briefs:
        connection.execute(
            """
            CREATE TABLE item_briefs(
                item_id INTEGER PRIMARY KEY,source_hash TEXT,status TEXT,model TEXT,
                brief_json TEXT,generated_at TEXT,error TEXT,validation_hash TEXT
            )
            """
        )
    connection.execute(
        "INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
        (
            "fast",
            "FAST <Research>",
            "rss",
            "核心论文",
            "https://source.example/fast",
            "https://source.example/feed",
            "{}",
        ),
    )
    return connection


class ItemPageExportTests(unittest.TestCase):
    def test_exports_sharded_professional_brief_without_embedding_item_data_in_html(self):
        connection = database()
        canonical_key = "doi:10.1/example"
        hostile = '</script><img src=x onerror="alert(1)">'
        item_id = connection.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,normalized_title,url,doi,authors,venue,published_at,
                summary,topics_json,discovered_at,updated_at,baseline
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                canonical_key,
                "paper",
                "Hostile " + hostile,
                "hostile",
                "https://paper.example/read?a=1&b=2",
                "10.1/example",
                "A. Author",
                "FAST 2026",
                "2026-02-01T00:00:00Z",
                "The method schedules GC in idle windows. Original " + hostile,
                '["FTL","NAND 可靠性"]',
                "2026-07-19T00:00:00Z",
                "2026-07-19T01:00:00Z",
                0,
            ),
        ).lastrowid
        connection.execute(
            "INSERT INTO item_sources VALUES(?,?,?,?,?,?,?)",
            (
                "fast",
                "paper-1",
                item_id,
                "https://source.example/paper-1",
                "hash",
                "2026-07-19T00:00:00Z",
                "2026-07-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO item_versions VALUES(?,?,?,?,?,?,?,?)",
            (
                item_id,
                "fast",
                "hash",
                "2026-07-19T01:00:00Z",
                "Version title",
                "https://source.example/paper-1",
                "2026-02-01T00:00:00Z",
                "Version summary",
            ),
        )
        connection.execute("INSERT INTO runs VALUES(1,'2026-07-19T00:00:00Z','ok')")
        connection.execute(
            "INSERT INTO run_events VALUES(?,?,?,?,?,?,?)",
            (
                1,
                item_id,
                "fast",
                "updated",
                "2026-07-19T01:00:00Z",
                "2026-07-19T01:05:00Z",
                None,
            ),
        )
        brief = {
            "title_zh": "面向 SSD 的结构化研究",
            "one_liner": "一句话 " + hostile,
            "what_it_is": "研究内容",
            "problem": "待解决问题",
            "core_idea": "核心思想",
            "supporting_quote": "The method schedules GC in idle windows.",
            "mechanism": "机制",
            "evidence": "实验结果",
            "system_layers": ["Host/应用", "NVMe/FE", "FTL", "NAND", "ECC/LDPC"],
            "engineering_relevance": "工程相关性",
            "reading_guide": "先读图 2",
            "limitations": "适用边界",
            "evidence_level": "source_summary",
        }
        model = "openai/gpt-4.1-mini"
        validation_hash = briefs.professional_validation_hash(
            "source-hash", model, brief
        )
        connection.execute(
            "INSERT INTO item_briefs VALUES(?,?,?,?,?,?,?,?)",
            (
                item_id,
                "source-hash",
                "professional",
                model,
                json.dumps(brief, ensure_ascii=False),
                "2026-07-19T02:00:00Z",
                None,
                validation_hash,
            ),
        )
        connection.commit()

        with tempfile.TemporaryDirectory() as directory:
            result = item_page.export_item_pages(
                connection, Path(directory), "https://reader.example/ssd-radar"
            )
            public_id = hashlib.sha256(canonical_key.encode()).hexdigest()[:32]
            shard = Path(directory) / "items" / public_id[:2] / f"{public_id}.json"
            self.assertTrue(shard.is_file())
            payload = json.loads(shard.read_text(encoding="utf-8"))
            self.assertEqual(payload["public_id"], public_id)
            self.assertEqual(
                payload["page_url"],
                f"https://reader.example/ssd-radar/item.html?id={public_id}",
            )
            self.assertEqual(payload["brief"]["status"], "professional")
            self.assertTrue(payload["brief"]["is_professional"])
            self.assertEqual(payload["brief"]["model"], "openai/gpt-4.1-mini")
            self.assertEqual(payload["brief"]["generated_at"], "2026-07-19T02:00:00Z")
            self.assertEqual(
                payload["original_published_at"], "2026-02-01T00:00:00Z"
            )
            self.assertEqual(
                payload["brief"]["content"]["supporting_quote"],
                "The method schedules GC in idle windows.",
            )
            self.assertEqual(payload["brief"]["content"]["system_layers"], ["Host/应用", "NVMe/FE", "FTL", "NAND", "ECC/LDPC"])
            self.assertEqual(payload["events"][0]["event_type"], "updated")
            self.assertEqual(payload["events"][0]["event_id"], f"1:{item_id}:updated")
            self.assertEqual(payload["versions"][0]["source_name"], "FAST <Research>")
            self.assertEqual(payload["sources"][0]["category"], "核心论文")
            self.assertIn(hostile, payload["original_summary"])
            self.assertEqual(result["by_item_id"][item_id]["public_id"], public_id)

            page = (Path(directory) / "item.html").read_text(encoding="utf-8")
            self.assertNotIn(hostile, page)
            self.assertNotIn("innerHTML", page)
            self.assertIn(".textContent", page)
            self.assertIn('/^[a-f0-9]{32}$/.test(publicId)', page)
            self.assertIn('fetch(shardUrl', page)
            self.assertIn('"?event=" + encodeURIComponent(selectedEvent)', page)
            self.assertIn('href="https://reader.example/ssd-radar/"', page)
            self.assertIn("核心思想的原文依据", page)
            for heading in (
                "内容是什么",
                "要解决的问题",
                "核心思想",
                "机制 / 怎么做",
                "证据 / 结果",
                "位于 SSD 全链路哪一层",
                "与你的工程工作有什么关系",
                "怎么读最划算",
                "局限与阅读边界",
                "证据等级",
            ):
                self.assertIn(heading, page)
            self.assertIn("阅读原文", page)
            self.assertIn("整理模型：", page)
            self.assertIn("生成时间：", page)
            self.assertIn("AI 自动整理完成 · 未经人工全文复核", page)
            self.assertIn("official_excerpt", page)
            self.assertIn('role="status" aria-live="polite"', page)
            self.assertIn("<noscript>", page)
        connection.close()

    def test_missing_brief_uses_strict_non_inferential_fallback(self):
        connection = database(with_briefs=False)
        title = "Magic controller eliminates every NAND error forever"
        item_id = connection.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,normalized_title,url,doi,authors,venue,published_at,
                summary,topics_json,discovered_at,updated_at,baseline
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "source:magic",
                "paper",
                title,
                "magic controller",
                "javascript:alert(1)",
                None,
                None,
                None,
                None,
                None,
                "[]",
                "2026-07-19T00:00:00Z",
                "2026-07-19T00:00:00Z",
                1,
            ),
        ).lastrowid
        connection.execute(
            "INSERT INTO item_sources VALUES(?,?,?,?,?,?,?)",
            ("fast", "magic", item_id, "javascript:alert(2)", "x", "now", "now"),
        )
        connection.commit()

        with tempfile.TemporaryDirectory() as directory:
            public_id = item_page.public_item_id("source:magic")
            shard = Path(directory) / "items" / public_id[:2] / f"{public_id}.json"
            public_result = item_page.export_item_pages(
                connection, directory, "https://reader.example/"
            )
            self.assertEqual(public_result["item_count"], 0)
            self.assertFalse(shard.exists())
            item_page.export_item_pages(
                connection,
                directory,
                "https://reader.example/",
                professional_only=False,
            )
            payload = json.loads(
                shard.read_text(encoding="utf-8")
            )
        self.assertEqual(payload["brief"]["status"], "missing")
        self.assertFalse(payload["brief"]["is_professional"])
        self.assertEqual(payload["original_url"], "")
        brief_text = json.dumps(payload["brief"]["content"], ensure_ascii=False)
        self.assertNotIn(title, brief_text)
        self.assertIn("未依据标题推断", payload["brief"]["content"]["evidence_level"])
        self.assertIn("尚未完成专业整理", payload["brief"]["content"]["one_liner"])
        self.assertIn("原页面未提供", payload["brief"]["content"]["supporting_quote"])
        connection.close()

    def test_item_filter_limits_related_rows_and_cleans_only_generated_shards(self):
        connection = database(with_briefs=False)
        connection.execute("INSERT INTO runs VALUES(1,'2026-07-19T00:00:00Z','ok')")
        item_ids = []
        canonical_keys = []
        for number in (1, 2):
            canonical_key = f"source:filtered:{number}"
            canonical_keys.append(canonical_key)
            item_id = connection.execute(
                """
                INSERT INTO items(
                    canonical_key,item_type,title,normalized_title,url,doi,authors,venue,
                    published_at,summary,topics_json,discovered_at,updated_at,baseline
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    canonical_key,
                    "paper",
                    f"Filtered paper {number}",
                    f"filtered paper {number}",
                    f"https://paper.example/{number}",
                    None,
                    f"Author {number}",
                    "FAST",
                    f"202{number}-02-01T00:00:00Z",
                    f"Summary {number}",
                    "[]",
                    "2026-07-19T00:00:00Z",
                    "2026-07-19T01:00:00Z",
                    1,
                ),
            ).lastrowid
            item_ids.append(item_id)
            connection.execute(
                "INSERT INTO item_sources VALUES(?,?,?,?,?,?,?)",
                (
                    "fast",
                    f"paper-{number}",
                    item_id,
                    f"https://source.example/paper-{number}",
                    f"hash-{number}",
                    "2026-07-19T00:00:00Z",
                    "2026-07-19T01:00:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO item_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    "fast",
                    f"hash-{number}",
                    "2026-07-19T01:00:00Z",
                    f"Version {number}",
                    f"https://source.example/paper-{number}",
                    f"202{number}-02-01T00:00:00Z",
                    f"Summary {number}",
                ),
            )
            connection.execute(
                "INSERT INTO run_events VALUES(?,?,?,?,?,?,?)",
                (
                    1,
                    item_id,
                    "fast",
                    "updated",
                    f"2026-07-19T0{number}:00:00Z",
                    None,
                    None,
                ),
            )
        connection.execute("ALTER TABLE run_events ADD COLUMN suppressed_at TEXT")
        connection.execute(
            """
            INSERT INTO run_events(
                run_id,item_id,source_id,event_type,created_at,delivered_at,
                websub_published_at,suppressed_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                1,
                item_ids[0],
                "fast",
                "new",
                "2026-07-18T01:00:00Z",
                None,
                None,
                "2026-07-19T00:00:00Z",
            ),
        )
        connection.execute("ALTER TABLE items ADD COLUMN original_published_at TEXT")
        connection.execute(
            "UPDATE items SET original_published_at=? WHERE id=?",
            ("2020-12-31T00:00:00Z", item_ids[0]),
        )
        connection.commit()

        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            initial = item_page.export_item_pages(
                connection,
                site,
                "https://reader.example/radar/",
                professional_only=False,
            )
            self.assertEqual(initial["item_count"], 2)
            public_ids = [item_page.public_item_id(key) for key in canonical_keys]
            shards = [
                site / "items" / public_id[:2] / f"{public_id}.json"
                for public_id in public_ids
            ]
            self.assertTrue(all(path.is_file() for path in shards))

            # A valid-looking obsolete shard belongs to this exporter and must
            # be removed. Files outside the exact shard grammar are retained.
            stale_public_id = "f" * 32
            stale_shard = (
                site / "items" / stale_public_id[:2] / f"{stale_public_id}.json"
            )
            stale_shard.parent.mkdir(parents=True, exist_ok=True)
            stale_shard.write_text("{}\n", encoding="utf-8")
            unrelated = stale_shard.parent / "notes.json"
            unrelated.write_text("keep\n", encoding="utf-8")
            unrelated_dir_file = site / "items" / "misc" / "data.json"
            unrelated_dir_file.parent.mkdir(parents=True, exist_ok=True)
            unrelated_dir_file.write_text("keep\n", encoding="utf-8")

            filtered = item_page.export_item_pages(
                connection,
                site,
                "https://reader.example/radar/",
                item_ids=[item_ids[0], item_ids[0], 999_999],
                professional_only=False,
            )
            self.assertEqual(filtered["item_count"], 1)
            self.assertEqual(set(filtered["by_item_id"]), {item_ids[0]})
            self.assertTrue(shards[0].is_file())
            self.assertFalse(shards[1].exists())
            self.assertFalse(stale_shard.exists())
            self.assertTrue(unrelated.is_file())
            self.assertTrue(unrelated_dir_file.is_file())

            payload = json.loads(shards[0].read_text(encoding="utf-8"))
            self.assertEqual(
                payload["original_published_at"], "2020-12-31T00:00:00Z"
            )
            self.assertEqual(
                [source["external_id"] for source in payload["sources"]],
                ["paper-1"],
            )
            self.assertEqual(
                [version["title"] for version in payload["versions"]], ["Version 1"]
            )
            self.assertEqual(
                [event["event_id"] for event in payload["events"]],
                [f"1:{item_ids[0]}:updated"],
            )

            empty = item_page.export_item_pages(
                connection,
                site,
                "https://reader.example/radar/",
                item_ids=[],
            )
            self.assertEqual(empty["item_count"], 0)
            self.assertEqual(empty["by_item_id"], {})
            self.assertFalse(shards[0].exists())
            self.assertTrue(unrelated.is_file())
            self.assertTrue(unrelated_dir_file.is_file())
            self.assertTrue((site / "item.html").is_file())
        connection.close()

    def test_ungrounded_professional_brief_is_downgraded_to_strict_fallback(self):
        connection = database()
        item_id = connection.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,normalized_title,url,doi,authors,venue,published_at,
                summary,topics_json,discovered_at,updated_at,baseline
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "source:broken-professional",
                "paper",
                "A title must not fill missing analysis",
                "a title",
                "https://paper.example/broken",
                None,
                None,
                None,
                None,
                "Source summary",
                "[]",
                "2026-07-19T00:00:00Z",
                "2026-07-19T00:00:00Z",
                0,
            ),
        ).lastrowid
        # The structure and attestation are valid, but the evidence quote is
        # absent from the stored source summary. A detail-only exporter must
        # enforce the same evidence grounding as the complete site build.
        ungrounded = {
            "title_zh": "看似完整",
            "one_liner": "不应发布的模型结论",
            "what_it_is": "内容",
            "problem": "问题",
            "core_idea": "思想",
            "supporting_quote": "A quote absent from the stored source summary.",
            "mechanism": "机制",
            "evidence": "证据",
            "system_layers": ["FTL"],
            "engineering_relevance": "相关性",
            "reading_guide": "阅读",
            "limitations": "局限",
            "evidence_level": "source_summary",
        }
        model = "openai/gpt-4.1-mini"
        validation_hash = briefs.professional_validation_hash(
            "source-hash", model, ungrounded
        )
        connection.execute(
            "INSERT INTO item_briefs VALUES(?,?,?,?,?,?,?,?)",
            (
                item_id,
                "source-hash",
                "professional",
                model,
                json.dumps(ungrounded, ensure_ascii=False),
                "2026-07-19T02:00:00Z",
                None,
                validation_hash,
            ),
        )
        connection.commit()

        with tempfile.TemporaryDirectory() as directory:
            public_id = item_page.public_item_id("source:broken-professional")
            shard = Path(directory) / "items" / public_id[:2] / f"{public_id}.json"
            public_result = item_page.export_item_pages(
                connection, directory, "https://reader.example/"
            )
            self.assertEqual(public_result["item_count"], 0)
            self.assertFalse(shard.exists())
            item_page.export_item_pages(
                connection,
                directory,
                "https://reader.example/",
                professional_only=False,
            )
            payload = json.loads(
                shard.read_text(encoding="utf-8")
            )

        self.assertEqual(payload["brief"]["status"], "failed")
        self.assertFalse(payload["brief"]["is_professional"])
        self.assertNotIn("不应发布的模型结论", json.dumps(payload["brief"]["content"], ensure_ascii=False))
        self.assertIn("尚未完成专业整理", payload["brief"]["content"]["one_liner"])
        connection.close()

    def test_page_url_accepts_an_event_and_rejects_non_public_base(self):
        canonical_key = "doi:10.2/a"
        public_id = item_page.public_item_id(canonical_key)
        self.assertEqual(
            item_page.item_page_url(canonical_key, "https://example.com/radar", "7:updated"),
            f"https://example.com/radar/item.html?id={public_id}&event=7%3Aupdated",
        )
        with self.assertRaises(ValueError):
            item_page.item_page_url(canonical_key, "file:///tmp/site")


if __name__ == "__main__":
    unittest.main()
