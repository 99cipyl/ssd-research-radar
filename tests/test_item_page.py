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
                "Original " + hostile,
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
            item_page.export_item_pages(connection, directory, "https://reader.example/")
            public_id = item_page.public_item_id("source:magic")
            payload = json.loads(
                (Path(directory) / "items" / public_id[:2] / f"{public_id}.json").read_text(
                    encoding="utf-8"
                )
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

    def test_malformed_professional_brief_is_downgraded_to_strict_fallback(self):
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
        # This object omits supporting_quote.  A loose "has some fields"
        # check would previously have exposed it as a professional brief.
        malformed = {
            "title_zh": "看似完整",
            "one_liner": "不应发布的模型结论",
            "what_it_is": "内容",
            "problem": "问题",
            "core_idea": "思想",
            "mechanism": "机制",
            "evidence": "证据",
            "system_layers": ["FTL"],
            "engineering_relevance": "相关性",
            "reading_guide": "阅读",
            "limitations": "局限",
            "evidence_level": "source_summary",
        }
        connection.execute(
            "INSERT INTO item_briefs VALUES(?,?,?,?,?,?,?,?)",
            (
                item_id,
                "source-hash",
                "professional",
                "openai/gpt-4.1-mini",
                json.dumps(malformed, ensure_ascii=False),
                "2026-07-19T02:00:00Z",
                None,
                None,
            ),
        )
        connection.commit()

        with tempfile.TemporaryDirectory() as directory:
            item_page.export_item_pages(connection, directory, "https://reader.example/")
            public_id = item_page.public_item_id("source:broken-professional")
            payload = json.loads(
                (Path(directory) / "items" / public_id[:2] / f"{public_id}.json").read_text(
                    encoding="utf-8"
                )
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
