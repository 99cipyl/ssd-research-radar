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
        "what_it_is": "围绕存储系统的研究论文。",
        "problem": "摘要指出垃圾回收会放大尾延迟。",
        "core_idea": "根据摘要，作者将回收工作拆分并调度到低负载窗口。",
        "supporting_quote": quote,
        "mechanism": "摘要披露了负载检测与分阶段回收等步骤。",
        "evidence": "摘要报告了与基线对比的尾延迟下降，但未给出完整实验配置。",
        "limitations": "原页面未提供全文实验配置和失效边界。",
        "system_layers": ["待判定"],
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
        self.conn.execute(
            "UPDATE items SET title='Reliable NAND Read Disturb' WHERE id=?",
            (second,),
        )
        self.conn.commit()
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
        user_payload = json.loads(request_body["messages"][1]["content"])
        supplied = user_payload["items"]
        self.assertEqual([item["item_id"] for item in supplied], [second, first])
        self.assertEqual(
            user_payload["validation_constraints"]["exact_item_count"], 2
        )
        self.assertIn(
            "GC/磨损均衡",
            user_payload["validation_constraints"]["allowed_system_layers"],
        )
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

    def test_rate_limit_retries_same_batch_once_with_retry_after(self):
        item_id = self.add_item(summary="Rate limited evidence sentence.")
        briefs.ensure_fallback_briefs(self.conn)
        stable_id = briefs.public_id("doi:10.1/test")
        response = {
            "choices": [{"message": {"content": json.dumps({
                "briefs": [professional(
                    item_id, stable_id, "Rate limited evidence sentence."
                )]
            }, ensure_ascii=False)}}]
        }
        rate_limit = urllib.error.HTTPError(
            briefs.GITHUB_MODELS_ENDPOINT,
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            None,
        )
        with mock.patch(
            "briefs._model_urlopen",
            side_effect=[rate_limit, FakeResponse(response)],
        ) as urlopen:
            result = briefs.generate_professional_briefs(
                self.conn,
                "token",
                priority_item_ids=[item_id],
                request_interval_seconds=0,
                max_rate_limit_retries=1,
            )

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["generated_item_ids"], [item_id])
        self.assertEqual(briefs.brief_for_item(self.conn, item_id)["status"], "professional")

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

    def test_history_backfill_reserves_progress_for_fresh_and_retry_rows(self):
        retry_ids = [
            self.add_item(key=f"doi:10.1/retry-{index}", summary="Retry evidence.")
            for index in range(6)
        ]
        fresh_ids = [
            self.add_item(key=f"doi:10.1/fresh-{index}", summary="Fresh evidence.")
            for index in range(6)
        ]
        briefs.ensure_fallback_briefs(self.conn)
        self.conn.executemany(
            "UPDATE item_briefs SET status='retry',last_attempt_at=NULL WHERE item_id=?",
            [(item_id,) for item_id in retry_ids],
        )
        self.conn.commit()

        candidates = briefs._candidate_rows(
            self.conn, [], history_limit=5, retry_after_seconds=0
        )

        self.assertEqual(len(candidates), 5)
        self.assertEqual([row["status"] for row in candidates[:4]], ["fallback"] * 4)
        self.assertEqual(candidates[4]["status"], "retry")
        self.assertTrue({row["id"] for row in candidates[:4]} <= set(fresh_ids))
        self.assertIn(candidates[4]["id"], retry_ids)

    def test_duplicate_titles_are_never_sent_in_the_same_model_batch(self):
        candidates = [
            {"id": 1, "title": "Repeated subject"},
            {"id": 2, "title": "Repeated subject"},
            {"id": 3, "title": "Different subject"},
            {"id": 4, "title": "Repeated subject"},
        ]
        batches = briefs._candidate_batches(candidates, 2)
        self.assertEqual(
            [[item["id"] for item in batch] for batch in batches],
            [[1], [2, 3], [4]],
        )
        for batch in batches:
            titles = [item["title"].casefold() for item in batch]
            self.assertEqual(len(titles), len(set(titles)))

    def test_extra_or_duplicate_model_items_fail_the_entire_batch_closed(self):
        item_id = self.add_item(summary="Fresh evidence sentence.")
        briefs.ensure_fallback_briefs(self.conn)
        stable_id = self.conn.execute(
            "SELECT public_id FROM item_briefs WHERE item_id=?", (item_id,)
        ).fetchone()[0]
        valid = professional(item_id, stable_id, "Fresh evidence sentence.")
        extra = dict(valid, item_id=item_id + 1000, public_id="unexpected")
        response = {
            "choices": [{"message": {"content": json.dumps({
                "briefs": [valid, extra]
            }, ensure_ascii=False)}}]
        }
        with mock.patch("briefs._model_urlopen", return_value=FakeResponse(response)):
            result = briefs.generate_professional_briefs(
                self.conn, "token", priority_item_ids=[item_id]
            )
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["failed_item_ids"], [item_id])
        self.assertIn("does not exactly match", " ".join(result["errors"]))
        self.assertEqual(briefs.brief_for_item(self.conn, item_id)["status"], "retry")

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
            "UPDATE items SET title=? WHERE id=?",
            [
                ("Safari article", safari),
                ("OpenAlex paper", openalex),
                ("OCP message", ocp),
            ],
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

        self.assertEqual(result["generated"], 2)
        self.assertEqual(result["failed_item_ids"], [ocp])
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
        self.assertEqual(briefs.brief_for_item(self.conn, ocp)["status"], "retry")
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

    def test_request_budget_truncation_preserves_evidence_provenance(self):
        common = {
            "title": "A long SSD paper",
            "item_type": "paper",
            "authors": "A. Author",
            "venue": "FAST",
            "published_at": "2026-02-01",
            "doi": "10.1/long",
            "url": "https://example.com/long",
        }
        source_summary = {
            **common,
            "id": 1,
            "public_id": "source-summary",
            "_evidence_text": "OpenAlex abstract sentence. " * 1_200,
            "_evidence_level": "source_summary",
        }
        official_fulltext = {
            **common,
            "id": 2,
            "public_id": "official-fulltext",
            "_evidence_text": "Official article sentence. " * 1_200,
            "_evidence_level": "official_fulltext",
        }

        fitted, payload = briefs._fit_request_budget(
            briefs.DEFAULT_MODEL,
            [source_summary, official_fulltext],
            7_000,
        )

        self.assertLessEqual(briefs._estimated_input_tokens(payload), 7_000)
        by_id = {item["id"]: item for item in fitted}
        self.assertTrue(by_id[1]["_evidence_truncated"])
        self.assertTrue(by_id[2]["_evidence_truncated"])
        self.assertEqual(by_id[1]["_evidence_level"], "source_summary")
        self.assertEqual(by_id[2]["_evidence_level"], "official_excerpt")

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

    def test_title_only_model_output_is_withheld_as_evidence_insufficient(self):
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
        self.assertEqual(result["generated"], 0)
        self.assertEqual(result["failed_item_ids"], [item_id])
        stored = briefs.brief_for_item(self.conn, item_id)
        self.assertEqual(stored["status"], "retry")
        self.assertNotIn("新型 FTL", stored["one_liner"])
        self.assertEqual(stored["system_layers"], ["待判定"])
        self.assertIn("不能仅凭题名推断", stored["core_idea"])

    def test_non_chinese_professional_narrative_is_rejected(self):
        claim = professional(1, "x", "The method reduces SSD latency.")
        claim["title_zh"] = "Reliable SSD Garbage Collection"
        with self.assertRaisesRegex(ValueError, "title_zh is not Chinese"):
            briefs.professional_validation_hash("source-hash", "model", claim)
        for technical_title in ("3D NAND", "P/E", "3D XPoint", "NVMe FDP"):
            claim["title_zh"] = technical_title
            briefs.professional_validation_hash("source-hash", "model", claim)
        claim["title_zh"] = "可靠的 SSD 垃圾回收"
        claim["one_liner"] = "This is an English-only model summary."
        with self.assertRaisesRegex(ValueError, "not Chinese: one_liner"):
            briefs.professional_validation_hash("source-hash", "model", claim)

    def test_numeric_guard_uses_whole_tokens_across_all_claim_fields(self):
        self.assertEqual(briefs._numeric_tokens("The improvement is 15%."), {"15", "15%"})
        self.assertEqual(briefs._numeric_tokens("The gain is 3x."), {"3", "3x"})
        self.assertEqual(briefs._numeric_tokens("A 3D NAND device."), {"3", "3d"})
        self.assertEqual(briefs._numeric_tokens("吞吐量提高 3 倍。"), {"3", "3x"})
        self.assertEqual(briefs._numeric_tokens("A 3-fold gain."), {"3", "3x"})
        self.assertEqual(briefs._numeric_tokens("吞吐量提高百分之 15。"), {"15", "15%"})
        self.assertEqual(briefs._numeric_tokens("May improve reliability."), set())
        self.assertEqual(
            briefs._numeric_tokens("五月份改进可靠性。"), {"month:5"}
        )
        self.assertEqual(
            briefs._numeric_tokens("The defense was held on May 26, 2026."),
            {"date:2026-05-26", "year:2026", "month:5", "day:26"},
        )
        self.assertEqual(
            briefs._numeric_tokens("We attended events in June & July 2026."),
            {
                "year:2026", "month:6", "month:7",
                "year-month:2026-06", "year-month:2026-07",
            },
        )
        self.assertEqual(
            briefs._numeric_tokens("Jul 13 Mon Storage Project Call"),
            {"month-day:07-13", "month:7", "day:13"},
        )
        self.assertEqual(
            briefs._numeric_tokens("5月26日"),
            {"month-day:05-26", "month:5", "day:26"},
        )
        self.assertEqual(briefs._numeric_tokens("7/13"), {"7", "13"})
        self.assertNotIn("month:7", briefs._numeric_tokens("7/13"))
        self.assertNotIn("5", briefs._numeric_tokens("May 26, 2026"))
        self.assertEqual(
            briefs._numeric_tokens("五月二十六日"),
            {"month-day:05-26", "month:5", "day:26"},
        )
        self.assertFalse(
            {"5", "26"} & briefs._numeric_tokens("五月二十六日")
        )
        self.assertEqual(
            briefs._numeric_tokens("百分之六十二"), {"62", "62%"}
        )
        self.assertEqual(
            briefs._numeric_tokens("62.8%"),
            briefs._numeric_tokens("百分之62.8"),
        )
        self.assertEqual(
            briefs._numeric_tokens("三个工作负载"),
            briefs._numeric_tokens("three workloads"),
        )
        for phrase in (
            "三篇论文", "两类方法", "四台设备", "五条结论",
            "六章内容", "七节实验", "八名作者",
        ):
            self.assertTrue(briefs._numeric_tokens(phrase), phrase)
        self.assertNotEqual(
            briefs._numeric_tokens("5个"), briefs._numeric_tokens("5次")
        )
        self.assertIn("1.5gbyte/s", briefs._numeric_tokens("1.5 GB/s"))
        self.assertNotIn("1.5gbyte/s", briefs._numeric_tokens("1.5 TB/s"))
        self.assertEqual(
            briefs._numeric_tokens("5 us"), briefs._numeric_tokens("5微秒")
        )
        self.assertEqual(
            briefs._numeric_tokens("100,000 IOPS"),
            briefs._numeric_tokens("十万 IOPS"),
        )
        self.assertEqual(
            briefs._numeric_tokens("100 GB/s"),
            briefs._numeric_tokens("一百 GB/s"),
        )
        self.assertEqual(
            briefs._numeric_tokens("100 GB/s"),
            briefs._numeric_tokens("one hundred GB/s"),
        )
        self.assertEqual(
            briefs._numeric_tokens("1.5 GB/s"),
            briefs._numeric_tokens("一点五 GB/s"),
        )
        self.assertEqual(briefs._numeric_tokens("分阶段回收"), set())
        for indefinite in (
            "这是一篇论文。", "这是一类方法。", "这是一台设备。",
            "这是一条结论。", "这是一章内容。", "这是一节实验。",
            "这是一名作者。", "这是一项研究。", "这是一种方案。",
        ):
            self.assertEqual(briefs._numeric_tokens(indefinite), set(), indefinite)
        self.assertIn("2paper", briefs._numeric_tokens("第二篇论文"))
        self.assertNotEqual(
            briefs._numeric_tokens("5 us"), briefs._numeric_tokens("5纳秒")
        )
        self.assertNotEqual(
            briefs._numeric_tokens("5 W"), briefs._numeric_tokens("5 J")
        )
        self.assertEqual(
            briefs._numeric_tokens("2012-04-01"),
            briefs._numeric_tokens("2012年4月1日"),
        )
        timestamp = briefs._numeric_tokens("2026-05-12T16:37:06Z")
        self.assertTrue(
            {
                "date:2026-05-12", "year:2026", "month:5", "day:12",
                "time:16:37:06", "hour:16", "minute:37", "second:6",
            }.issubset(timestamp)
        )
        self.assertFalse({"2026", "5", "12", "16", "37", "6"} & timestamp)
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
        self.assertIn("numeric claim '5", " ".join(result["errors"]))

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
        claim["evidence"] = "该设计实现3倍吞吐提升。"
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
        claim["evidence"] = "该设计声称吞吐提升3倍。"
        with self.assertRaisesRegex(ValueError, "3x"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        claim = professional(1, "x", "We test configuration 3, then compare baselines.")
        claim["title_zh"] = "性能提高 99% 的 NAND 调度方法"
        with self.assertRaisesRegex(ValueError, "numeric title claim '99"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )

        month_item = dict(item)
        month_item["title"] = "Seminar on May 26, 2026"
        month_item["summary"] = "The seminar is scheduled for May 26, 2026."
        month_item["_evidence_text"] = month_item["summary"]
        month_claim = professional(
            1, "x", "The seminar is scheduled for May 26, 2026."
        )
        month_claim["title_zh"] = "2026年5月26日研讨会"
        month_claim["one_liner"] = "该研讨会安排在2026年5月26日。"
        guarded_month = briefs._apply_provenance_guard(
            briefs.parse_brief(month_claim), "source_summary", month_item
        )
        self.assertEqual(guarded_month["title_zh"], "2026年5月26日研讨会")

        count_claim = professional(
            1, "x", "The seminar is scheduled for May 26, 2026."
        )
        count_claim["title_zh"] = "2026年5月26日研讨会"
        count_claim["one_liner"] = "该资料讨论5个方案。"
        with self.assertRaisesRegex(ValueError, "numeric claim '5"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(count_claim), "source_summary", month_item
            )

        for invented_count in ("该资料评估26个方案。", "该资料执行2026次测试。"):
            count_claim["one_liner"] = invented_count
            with self.assertRaisesRegex(ValueError, "numeric claim"):
                briefs._apply_provenance_guard(
                    briefs.parse_brief(count_claim), "source_summary", month_item
                )

        month_item["published_at"] = "2012-04-01T00:00:00Z"
        metadata_claim = professional(
            1, "x", "The seminar is scheduled for May 26, 2026."
        )
        metadata_claim["title_zh"] = "2026年5月26日研讨会"
        metadata_claim["what_it_is"] = "该资料发表于2012年4月1日。"
        briefs._apply_provenance_guard(
            briefs.parse_brief(metadata_claim), "source_summary", month_item
        )

        crossed_item = dict(month_item)
        crossed_item["title"] = "Two event dates"
        crossed_item["summary"] = "The events occur on May 1 and June 2."
        crossed_item["_evidence_text"] = crossed_item["summary"]
        crossed_claim = professional(
            1, "x", "The events occur on May 1 and June 2."
        )
        crossed_claim["title_zh"] = "两个活动日期"
        crossed_claim["one_liner"] = "活动安排在6月1日。"
        with self.assertRaisesRegex(ValueError, "month-day:06-01"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(crossed_claim), "source_summary", crossed_item
            )

        unit_item = dict(crossed_item)
        unit_item["summary"] = "Measured throughput reaches 1.5 GB/s."
        unit_item["_evidence_text"] = unit_item["summary"]
        unit_claim = professional(
            1, "x", "Measured throughput reaches 1.5 GB/s."
        )
        unit_claim["evidence"] = "测得吞吐量达到1.5 TB/s。"
        with self.assertRaisesRegex(ValueError, "1.5tbyte/s"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(unit_claim), "source_summary", unit_item
            )

        word_number_item = dict(crossed_item)
        word_number_item["summary"] = "No quantitative result is provided."
        word_number_item["_evidence_text"] = word_number_item["summary"]
        word_number_claim = professional(
            1, "x", "No quantitative result is provided."
        )
        word_number_claim["evidence"] = "吞吐量提高三倍。"
        with self.assertRaisesRegex(ValueError, "numeric claim"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(word_number_claim),
                "source_summary",
                word_number_item,
            )

    def test_layer_labels_must_be_exact_and_database_gc_is_not_ssd_gc(self):
        item = {
            "id": 1,
            "title": "Scalable Garbage Collection for Multi-Version OLTP",
            "summary": "The database reclaims obsolete tuple versions under MVCC.",
            "public_id": "x",
            "source_hash": "hash",
            "_evidence_text": "The database reclaims obsolete tuple versions under MVCC.",
            "_evidence_level": "source_summary",
        }
        claim = professional(
            1, "x", "The database reclaims obsolete tuple versions under MVCC."
        )
        claim["system_layers"] = ["FTL / GC"]
        with self.assertRaisesRegex(ValueError, "unsupported system_layers"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        claim["system_layers"] = ["GC/磨损均衡"]
        with self.assertRaisesRegex(ValueError, "database/runtime GC"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        claim["system_layers"] = ["ECC/LDPC"]
        with self.assertRaisesRegex(ValueError, "ECC/LDPC.*absent"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )
        claim["system_layers"] = ["Host/应用"]
        self.assertEqual(
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )["system_layers"],
            ["Host/应用"],
        )
        claim["system_layers"] = ["NAND"]
        with self.assertRaisesRegex(ValueError, "NAND.*absent"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(claim), "source_summary", item
            )

        ecc_item = dict(item)
        ecc_item["title"] = "LDPC decoding for NAND flash"
        ecc_item["summary"] = "The LDPC decoder corrects NAND read errors."
        ecc_item["_evidence_text"] = ecc_item["summary"]
        ecc_claim = professional(
            1, "x", "The LDPC decoder corrects NAND read errors."
        )
        ecc_claim["system_layers"] = ["ECC/LDPC"]
        self.assertEqual(
            briefs._apply_provenance_guard(
                briefs.parse_brief(ecc_claim), "source_summary", ecc_item
            )["system_layers"],
            ["ECC/LDPC"],
        )

        incidental_ssd_item = dict(item)
        incidental_ssd_item["summary"] = (
            "The MVCC database garbage collector runs on an SSD-backed server."
        )
        incidental_ssd_item["_evidence_text"] = incidental_ssd_item["summary"]
        incidental_claim = professional(
            1,
            "x",
            "The MVCC database garbage collector runs on an SSD-backed server.",
        )
        incidental_claim["system_layers"] = ["GC/磨损均衡"]
        with self.assertRaisesRegex(ValueError, "database/runtime GC"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(incidental_claim),
                "source_summary",
                incidental_ssd_item,
            )

        ssd_gc_item = dict(item)
        ssd_gc_item["title"] = "NAND FTL Garbage Collection"
        ssd_gc_item["summary"] = (
            "The NAND FTL performs garbage collection of invalid pages."
        )
        ssd_gc_item["_evidence_text"] = ssd_gc_item["summary"]
        ssd_gc_claim = professional(
            1,
            "x",
            "The NAND FTL performs garbage collection of invalid pages.",
        )
        ssd_gc_claim["system_layers"] = ["GC/磨损均衡"]
        guarded_ssd_gc = briefs._apply_provenance_guard(
            briefs.parse_brief(ssd_gc_claim), "source_summary", ssd_gc_item
        )
        self.assertEqual(guarded_ssd_gc["system_layers"], ["GC/磨损均衡"])

        reliability_item = dict(item)
        reliability_item["title"] = "NAND Read Disturb"
        reliability_item["summary"] = (
            "Read disturb errors are measured across NAND erase blocks."
        )
        reliability_item["_evidence_text"] = reliability_item["summary"]
        reliability_claim = professional(
            1,
            "x",
            "Read disturb errors are measured across NAND erase blocks.",
        )
        reliability_claim["system_layers"] = ["NAND", "可靠性"]
        self.assertEqual(
            briefs._apply_provenance_guard(
                briefs.parse_brief(reliability_claim),
                "source_summary",
                reliability_item,
            )["system_layers"],
            ["NAND", "可靠性"],
        )
        reliability_claim["system_layers"] = ["GC/磨损均衡"]
        with self.assertRaisesRegex(ValueError, "explicit SSD/NAND/FTL evidence"):
            briefs._apply_provenance_guard(
                briefs.parse_brief(reliability_claim),
                "source_summary",
                reliability_item,
            )

        claim["system_layers"] = ["Host/应用"]
        guarded = briefs._apply_provenance_guard(
            briefs.parse_brief(claim), "source_summary", item
        )
        self.assertEqual(guarded["system_layers"], ["Host/应用"])
        claim["system_layers"] = ["Imaginary controller layer"]
        with self.assertRaisesRegex(ValueError, "unsupported system_layers"):
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
        valid = professional(
            item_id, row["public_id"], "The source says only this sentence."
        )
        model = "test-model"
        validation_hash = briefs.professional_validation_hash(
            row["source_hash"], model, valid
        )
        malicious = dict(valid)
        malicious["one_liner"] = "FABRICATED LEAK"
        malicious["system_layers"] = ["MADE-UP-LAYER"]
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
