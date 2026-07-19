import hashlib
import json
import sqlite3
import unittest
import urllib.error
from unittest import mock

import briefs


ITEMS_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL DEFAULT '',
    url TEXT,
    doi TEXT,
    authors TEXT,
    venue TEXT,
    published_at TEXT,
    summary TEXT,
    topics_json TEXT NOT NULL DEFAULT '[]',
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE item_sources (
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    item_id INTEGER NOT NULL REFERENCES items(id),
    source_url TEXT,
    raw_hash TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT,
    last_seen_at TEXT,
    PRIMARY KEY(source_id, external_id)
);
"""


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


def professional(item_id, public_id, quote="GC creates tail latency."):
    return {
        "item_id": item_id,
        "public_id": public_id,
        "title_zh": "面向 SSD 的可靠垃圾回收",
        "one_liner": "该工作在给定摘要范围内讨论了降低垃圾回收尾延迟的方法。",
        "what_it_is": "一项围绕 FTL 垃圾回收的研究论文。",
        "problem": "摘要指出垃圾回收会放大尾延迟。",
        "core_idea": "根据摘要，作者将回收工作拆分并调度到低负载窗口。",
        "supporting_quote": quote,
        "mechanism": "摘要披露了负载检测与分阶段回收两个步骤。",
        "evidence": "摘要报告了与基线对比的尾延迟下降，但未给出完整实验配置。",
        "limitations": "原页面未提供全文实验配置和失效边界。",
        "system_layers": ["FTL", "NMT/块管理", "GC/磨损均衡"],
        "engineering_relevance": "可用于审视前后台调度与块回收的相互影响。",
        "reading_guide": "阅读原文时重点核对回收触发条件、并发控制和评价基线。",
        "evidence_level": "题名+摘要",
    }


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(ITEMS_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def add_item(self, key="doi:10.1/test", summary="", topics=None):
        cursor = self.conn.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,url,authors,venue,published_at,
                summary,topics_json,discovered_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                "paper",
                "Reliable SSD Garbage Collection",
                "https://example.com/paper",
                "A. Author",
                "FAST",
                "2026-02-01",
                summary,
                json.dumps(topics or [], ensure_ascii=False),
                "2026-07-19T00:00:00+00:00",
                "2026-07-19T00:00:00+00:00",
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def test_fallback_is_stable_and_strict_when_abstract_is_missing(self):
        item_id = self.add_item()
        counts = briefs.ensure_fallback_briefs(self.conn)

        self.assertEqual(counts, {"created": 1, "refreshed": 0, "unchanged": 0})
        brief = briefs.brief_for_item(self.conn, item_id)
        expected_id = hashlib.sha256(b"doi:10.1/test").hexdigest()[:32]
        self.assertEqual(brief["public_id"], expected_id)
        self.assertEqual(brief["status"], "fallback")
        self.assertEqual(brief["system_layers"], ["待判定"])
        self.assertIn("不能仅凭题名推断", brief["core_idea"])
        self.assertIn("仅题名", brief["evidence_level"])
        legacy_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)
        ).fetchone()
        self.assertIn("不能仅凭题名推断", briefs.fallback_brief(legacy_row)["core_idea"])
        self.assertEqual(
            briefs.ensure_fallback_briefs(self.conn),
            {"created": 0, "refreshed": 0, "unchanged": 1},
        )

    def test_changed_source_invalidates_professional_brief(self):
        item_id = self.add_item(
            summary="GC creates tail latency. The design schedules work in idle windows.",
            topics=["FTL / GC"],
        )
        briefs.ensure_fallback_briefs(self.conn)
        row = self.conn.execute(
            "SELECT public_id FROM item_briefs WHERE item_id=?", (item_id,)
        ).fetchone()
        self.conn.execute(
            "UPDATE item_briefs SET status='professional',model='test-model' WHERE item_id=?",
            (item_id,),
        )
        self.conn.execute(
            "UPDATE items SET summary=? WHERE id=?",
            ("A materially changed abstract.", item_id),
        )
        self.conn.commit()

        counts = briefs.ensure_fallback_briefs(self.conn)
        refreshed = briefs.brief_for_item(self.conn, row["public_id"])

        self.assertEqual(counts["refreshed"], 1)
        self.assertEqual(refreshed["status"], "fallback")
        self.assertIsNone(refreshed["model"])
        self.assertIn("不直接复制原始摘录", refreshed["core_idea"])

    def test_changed_upstream_hash_invalidates_brief_when_excerpt_is_unchanged(self):
        item_id = self.add_item(summary="An unchanged short WordPress excerpt.")
        self.conn.execute(
            "INSERT INTO item_sources(source_id,external_id,item_id,raw_hash) VALUES(?,?,?,?)",
            ("safari_eth", "post-1", item_id, "body-v1"),
        )
        self.conn.commit()
        briefs.ensure_fallback_briefs(self.conn)
        self.conn.execute(
            "UPDATE item_briefs SET status='professional',model='test' WHERE item_id=?",
            (item_id,),
        )
        self.conn.execute(
            "UPDATE item_sources SET raw_hash='body-v2' WHERE item_id=?", (item_id,)
        )
        self.conn.commit()

        counts = briefs.ensure_fallback_briefs(self.conn)
        refreshed = briefs.brief_for_item(self.conn, item_id)
        self.assertEqual(counts["refreshed"], 1)
        self.assertEqual(refreshed["status"], "fallback")
        self.assertIsNone(refreshed["model"])

    def test_parse_rejects_incomplete_or_malformed_briefs(self):
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            briefs.parse_brief("not json")
        with self.assertRaisesRegex(ValueError, "one_liner"):
            briefs.parse_brief({"title_zh": "only one field"})
        invalid = professional(1, "x")
        invalid["system_layers"] = "FTL"
        with self.assertRaisesRegex(ValueError, "non-empty array"):
            briefs.parse_brief(invalid)

    def test_successful_batch_generation_uses_grounded_prompt_and_persists(self):
        first = self.add_item(
            key="doi:10.1/one",
            summary="GC creates tail latency. The method schedules GC in idle windows.",
            topics=["FTL / GC"],
        )
        second = self.add_item(
            key="doi:10.1/two",
            summary="Read disturb raises NAND errors. Results are not included here.",
            topics=["NAND 可靠性"],
        )
        briefs.ensure_fallback_briefs(self.conn)
        ids = {
            row["item_id"]: row["public_id"]
            for row in self.conn.execute("SELECT item_id,public_id FROM item_briefs")
        }
        response_content = {
            "briefs": [
                professional(first, ids[first], "The method schedules GC in idle windows."),
                professional(second, ids[second], "Read disturb raises NAND errors."),
            ]
        }
        response = {
            "choices": [{"message": {"content": json.dumps(response_content, ensure_ascii=False)}}]
        }

        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(response)

        with mock.patch("briefs._model_urlopen", side_effect=fake_urlopen):
            result = briefs.generate_professional_briefs(
                self.conn,
                "secret-token",
                model="openai/test",
                priority_item_ids=[second, first],
                batch_size=4,
            )

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["generated_item_ids"], [second, first])
        request = captured["request"]
        self.assertEqual(request.full_url, briefs.GITHUB_MODELS_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertIn("不得", request_body["messages"][0]["content"])
        supplied = json.loads(request_body["messages"][1]["content"])["items"]
        self.assertEqual([item["item_id"] for item in supplied], [second, first])
        stored = briefs.brief_for_item(self.conn, first)
        self.assertEqual(stored["status"], "professional")
        self.assertEqual(stored["model"], "openai/test")
        self.assertEqual(stored["evidence_level"], "source_summary")
        description = briefs.feed_description(stored, "https://example.com/original")
        self.assertIn("【核心思想】", description)
        self.assertIn("【核心思想原文依据】", description)
        self.assertIn("【SSD 全链路位置】", description)
        self.assertIn("https://example.com/original", description)

    def test_network_failure_keeps_fallback_and_is_retried(self):
        item_id = self.add_item(summary="The abstract provides limited evidence.")
        briefs.ensure_fallback_briefs(self.conn)
        fallback_json = self.conn.execute(
            "SELECT brief_json FROM item_briefs WHERE item_id=?", (item_id,)
        ).fetchone()[0]

        with mock.patch(
            "briefs._model_urlopen",
            side_effect=urllib.error.URLError("temporary outage"),
        ):
            failed = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=[item_id]
            )

        row = self.conn.execute(
            "SELECT status,brief_json,last_error,attempt_count FROM item_briefs WHERE item_id=?",
            (item_id,),
        ).fetchone()
        self.assertEqual(failed["failed_item_ids"], [item_id])
        self.assertEqual(row["status"], "retry")
        self.assertEqual(row["brief_json"], fallback_json)
        self.assertIn("temporary outage", row["last_error"])
        self.assertEqual(row["attempt_count"], 1)

        stable_id = briefs.public_id("doi:10.1/test")
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "briefs": [
                                    professional(
                                        item_id,
                                        stable_id,
                                        "The abstract provides limited evidence.",
                                    )
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        with mock.patch(
            "briefs._model_urlopen", return_value=FakeResponse(response)
        ):
            retried = briefs.generate_professional_briefs(
                self.conn, "token", history_limit=1, retry_after_seconds=0
            )

        self.assertEqual(retried["generated_item_ids"], [item_id])
        retried_row = self.conn.execute(
            "SELECT status,last_error,attempt_count FROM item_briefs WHERE item_id=?",
            (item_id,),
        ).fetchone()
        self.assertEqual(retried_row["status"], "professional")
        self.assertIsNone(retried_row["last_error"])
        self.assertEqual(retried_row["attempt_count"], 2)

    def test_missing_token_marks_candidates_for_retry_without_api_call(self):
        item_id = self.add_item(summary="An abstract is available.")
        with mock.patch("briefs._model_urlopen") as urlopen:
            result = briefs.generate_professional_briefs(
                self.conn, "", priority_item_ids=[item_id]
            )
        urlopen.assert_not_called()
        self.assertEqual(result["failed"], 1)
        brief = briefs.brief_for_item(self.conn, item_id)
        self.assertEqual(brief["status"], "retry")
        self.assertIn("not configured", brief["last_error"])

    def test_backoff_items_do_not_starve_a_later_fresh_priority(self):
        item_ids = [
            self.add_item(key=f"doi:10.1/priority-{index}", summary="Fresh evidence sentence.")
            for index in range(13)
        ]
        briefs.ensure_fallback_briefs(self.conn)
        self.conn.executemany(
            "UPDATE item_briefs SET status='retry',last_attempt_at=? WHERE item_id=?",
            [(briefs._now(), item_id) for item_id in item_ids[:12]],
        )
        self.conn.commit()
        last = item_ids[-1]
        stable_id = self.conn.execute(
            "SELECT public_id FROM item_briefs WHERE item_id=?", (last,)
        ).fetchone()[0]
        response = {
            "choices": [{"message": {"content": json.dumps({
                "briefs": [professional(last, stable_id, "Fresh evidence sentence.")]
            }, ensure_ascii=False)}}]
        }
        with mock.patch("briefs._model_urlopen", return_value=FakeResponse(response)):
            result = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=item_ids, max_priority_items=12
            )
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["generated_item_ids"], [last])

    def test_generation_enriches_only_selected_source_types(self):
        safari = self.add_item(
            key="url:safari",
            summary="Short Safari excerpt.",
            topics=["NAND 可靠性"],
        )
        openalex = self.add_item(
            key="doi:openalex",
            summary="Already complete OpenAlex abstract.",
            topics=["FTL / GC"],
        )
        ocp = self.add_item(
            key="url:ocp-empty",
            summary="",
            topics=["数据中心 / QoS"],
        )
        self.conn.execute(
            "UPDATE items SET url='https://safari.ethz.ch/example/' WHERE id=?",
            (safari,),
        )
        self.conn.executemany(
            "INSERT INTO item_sources(source_id,external_id,item_id) VALUES(?,?,?)",
            [
                ("safari_eth", "safari-1", safari),
                ("openalex_ssd", "oa-1", openalex),
                ("ocp_storage", "ocp-1", ocp),
            ],
        )
        self.conn.commit()
        briefs.ensure_fallback_briefs(self.conn)
        public_ids = {
            row["item_id"]: row["public_id"]
            for row in self.conn.execute("SELECT item_id,public_id FROM item_briefs")
        }
        model_result = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "briefs": [
                                    professional(
                                        safari,
                                        public_ids[safari],
                                        "Full official Safari article body",
                                    ),
                                    professional(
                                        openalex,
                                        public_ids[openalex],
                                        "Already complete OpenAlex abstract.",
                                    ),
                                    professional(
                                        ocp,
                                        public_ids[ocp],
                                        "原页面未提供",
                                    ),
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(model_result)

        extracted = {
            "text": "Full official Safari article body with its complete mechanism.",
            "evidence_level": "official_fulltext",
            "source_url": "https://safari.ethz.ch/full-article/",
        }
        with mock.patch(
            "briefs.evidence.enrich_item_evidence", return_value=extracted
        ) as enrich, mock.patch(
            "briefs._model_urlopen", side_effect=fake_urlopen
        ):
            result = briefs.generate_professional_briefs(
                self.conn,
                "token",
                priority_item_ids=[safari, openalex],
                history_limit=1,
                batch_size=3,
            )

        self.assertEqual(result["generated"], 3)
        enrich.assert_called_once()
        self.assertEqual(enrich.call_args.args[1], ["safari_eth"])
        model_items = json.loads(captured["payload"]["messages"][1]["content"])[
            "items"
        ]
        by_id = {item["item_id"]: item for item in model_items}
        self.assertEqual(
            by_id[safari]["summary_or_excerpt"], extracted["text"]
        )
        self.assertEqual(by_id[safari]["evidence_level"], "official_fulltext")
        self.assertEqual(
            by_id[safari]["evidence_source_url"], extracted["source_url"]
        )
        self.assertEqual(
            by_id[openalex]["summary_or_excerpt"],
            "Already complete OpenAlex abstract.",
        )
        self.assertEqual(
            briefs.brief_for_item(self.conn, safari)["evidence_level"],
            "official_fulltext",
        )
        self.assertEqual(by_id[ocp]["evidence_level"], "none")
        self.assertIn(
            "不能仅凭题名推断",
            briefs.brief_for_item(self.conn, ocp)["core_idea"],
        )

    def test_enrichment_does_not_follow_untrusted_hosts(self):
        candidate = {
            "id": 1,
            "url": "http://127.0.0.1/internal-metadata",
            "summary": "Short excerpt.",
            "source_ids": "safari_eth",
        }
        with mock.patch("briefs.evidence.enrich_item_evidence") as enrich:
            result = briefs._enrich_candidates([candidate])
        enrich.assert_not_called()
        self.assertEqual(result[0]["_evidence_text"], "Short excerpt.")
        self.assertEqual(result[0]["_evidence_level"], "source_summary")

        nvmw = dict(
            candidate,
            url="https://nvmw.ucsd.edu/program-4#paper-1",
            source_ids="nvmw_official",
            summary="Official Program abstract.",
        )
        with mock.patch("briefs.evidence.enrich_item_evidence") as enrich:
            enriched = briefs._enrich_candidates([nvmw])
        enrich.assert_not_called()
        self.assertEqual(enriched[0]["_evidence_level"], "official_abstract")

    def test_provenance_guard_rejects_invented_quote_and_layer(self):
        item_id = self.add_item(summary="The method reduces GC tail latency.")
        briefs.ensure_fallback_briefs(self.conn)
        stable_id = briefs.public_id("doi:10.1/test")
        fabricated = professional(item_id, stable_id, "A result that is not in evidence.")
        fabricated["system_layers"] = ["Imaginary controller layer"]
        response = {
            "choices": [{"message": {"content": json.dumps({"briefs": [fabricated]})}}]
        }
        with mock.patch("briefs._model_urlopen", return_value=FakeResponse(response)):
            result = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=[item_id]
            )
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["failed_item_ids"], [item_id])
        self.assertEqual(briefs.brief_for_item(self.conn, item_id)["status"], "retry")

    def test_title_only_model_output_is_replaced_by_strict_unknowns(self):
        item_id = self.add_item(summary="")
        briefs.ensure_fallback_briefs(self.conn)
        stable_id = briefs.public_id("doi:10.1/test")
        invented = professional(item_id, stable_id, "原页面未提供")
        invented["one_liner"] = "模型臆测这是一种新型 FTL。"
        invented["system_layers"] = ["FTL"]
        response = {
            "choices": [{"message": {"content": json.dumps({"briefs": [invented]}, ensure_ascii=False)}}]
        }
        with mock.patch("briefs._model_urlopen", return_value=FakeResponse(response)):
            result = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=[item_id]
            )
        self.assertEqual(result["generated"], 1)
        stored = briefs.brief_for_item(self.conn, item_id)
        self.assertNotIn("新型 FTL", stored["one_liner"])
        self.assertEqual(stored["system_layers"], ["待判定"])
        self.assertIn("不能仅凭题名推断", stored["core_idea"])

    def test_numeric_guard_uses_whole_tokens_across_all_claim_fields(self):
        self.assertEqual(briefs._numeric_tokens("The improvement is 15%."), {"15%"})
        self.assertEqual(briefs._numeric_tokens("The gain is 3x."), {"3x"})
        self.assertEqual(briefs._numeric_tokens("A 3D NAND device."), {"3d"})
        self.assertEqual(briefs._numeric_tokens("吞吐量提高 3 倍。"), {"3x"})
        self.assertEqual(briefs._numeric_tokens("A 3-fold gain."), {"3x"})
        self.assertEqual(briefs._numeric_tokens("吞吐量提高百分之 15。"), {"15", "15%"})
        item_id = self.add_item(summary="The measured improvement was 15%.")
        briefs.ensure_fallback_briefs(self.conn)
        stable_id = briefs.public_id("doi:10.1/test")
        invented = professional(
            item_id, stable_id, "The measured improvement was 15%."
        )
        invented["engineering_relevance"] = "工程收益可达到 5%。"
        response = {
            "choices": [{"message": {"content": json.dumps({"briefs": [invented]}, ensure_ascii=False)}}]
        }
        with mock.patch("briefs._model_urlopen", return_value=FakeResponse(response)):
            result = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=[item_id]
            )
        self.assertEqual(result["generated"], 0)
        self.assertIn("numeric claim '5%'", " ".join(result["errors"]))

        item = {
            "id": 1,
            "title": "A 3D NAND paper",
            "summary": "The abstract contains no quantitative result.",
            "public_id": "x",
            "source_hash": "hash",
            "_evidence_text": "The abstract contains no quantitative result.",
            "_evidence_level": "source_summary",
        }
        claim = professional(1, "x", "The abstract contains no quantitative result.")
        claim["evidence"] = "The design achieves a 3x throughput gain."
        with self.assertRaisesRegex(ValueError, "3x"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        item["_evidence_text"] = "We evaluate 3 workloads."
        item["summary"] = item["_evidence_text"]
        claim = professional(1, "x", "We evaluate 3 workloads.")
        claim["evidence"] = "吞吐量提高 3 倍。"
        with self.assertRaisesRegex(ValueError, "3x"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        item["_evidence_text"] = "We test configuration 3, then compare baselines."
        item["summary"] = item["_evidence_text"]
        claim = professional(1, "x", "We test configuration 3, then compare baselines.")
        claim["evidence"] = "The throughput gain is 3x."
        with self.assertRaisesRegex(ValueError, "3x"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        claim = professional(1, "x", "We test configuration 3, then compare baselines.")
        claim["title_zh"] = "性能提高 99% 的 NAND 调度方法"
        with self.assertRaisesRegex(ValueError, "numeric title claim '99%'"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )

    def test_invalid_professional_row_is_demoted_before_publish(self):
        item_id = self.add_item(summary="A source summary.")
        briefs.ensure_fallback_briefs(self.conn)
        self.conn.execute(
            "UPDATE item_briefs SET status='professional',brief_json='{}' WHERE item_id=?",
            (item_id,),
        )
        self.conn.commit()
        self.assertEqual(briefs.validate_professional_briefs(self.conn), 1)
        row = self.conn.execute(
            "SELECT status,last_error FROM item_briefs WHERE item_id=?", (item_id,)
        ).fetchone()
        self.assertEqual(row["status"], "retry")
        self.assertIn("invalid", row["last_error"])

    def test_demoted_professional_content_is_replaced_by_safe_fallback(self):
        item_id = self.add_item(summary="The source says only this sentence.")
        briefs.ensure_fallback_briefs(self.conn)
        row = self.conn.execute(
            "SELECT source_hash,public_id FROM item_briefs WHERE item_id=?", (item_id,)
        ).fetchone()
        malicious = professional(
            item_id, row["public_id"], "fabricated quote absent from evidence"
        )
        malicious["one_liner"] = "FABRICATED LEAK"
        malicious["system_layers"] = ["MADE-UP-LAYER"]
        model = "test-model"
        validation_hash = briefs.professional_validation_hash(
            row["source_hash"], model, malicious
        )
        self.conn.execute(
            """
            UPDATE item_briefs SET status='professional',model=?,brief_json=?,validation_hash=?
            WHERE item_id=?
            """,
            (model, json.dumps(malicious, ensure_ascii=False), validation_hash, item_id),
        )
        self.conn.commit()
        self.assertEqual(briefs.validate_professional_briefs(self.conn), 1)
        stored = briefs.brief_for_item(self.conn, item_id)
        self.assertEqual(stored["status"], "retry")
        self.assertNotIn("FABRICATED LEAK", json.dumps(stored, ensure_ascii=False))
        self.assertIn("不直接复制原始摘录", stored["core_idea"])


if __name__ == "__main__":
    unittest.main()
