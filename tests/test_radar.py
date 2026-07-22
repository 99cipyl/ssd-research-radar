import json
import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import radar


def mark_professional(connection, item_id=None):
    where = " WHERE item_id=?" if item_id is not None else ""
    parameters = (item_id,) if item_id is not None else ()
    for row in connection.execute(
        "SELECT b.item_id,b.source_hash,b.brief_json,i.title,i.summary "
        "FROM item_briefs b JOIN items i ON i.id=b.item_id" + where.replace("item_id", "b.item_id"),
        parameters,
    ).fetchall():
        content = json.loads(row["brief_json"])
        content["title_zh"] = f"中文：{row['title']}"
        if row["summary"]:
            content["evidence_level"] = "source_summary"
            content["system_layers"] = ["NAND"]
            content["supporting_quote"] = " ".join(str(row["summary"]).split()[:20])
        else:
            content["evidence_level"] = "none"
            content["supporting_quote"] = "原页面未提供"
            content["system_layers"] = ["待判定"]
        model = "test-model"
        validation_hash = radar.briefs.professional_validation_hash(
            row["source_hash"], model, content
        )
        connection.execute(
            "UPDATE item_briefs SET status='professional',model=?,brief_json=?,validation_hash=? WHERE item_id=?",
            (model, json.dumps(content, ensure_ascii=False), validation_hash, row["item_id"]),
        )
    connection.commit()


class RadarUnitTests(unittest.TestCase):
    @staticmethod
    def dblp_fixture_responses():
        index = b"""<bht>
        <dblpcites><r><proceedings key='conf/fast/2024'><year>2024</year>
          <url>db/conf/fast/fast2024.html</url></proceedings></r></dblpcites>
        <dblpcites><r><proceedings key='conf/fast/2026'><year>2026</year>
          <url>db/conf/fast/fast2026.html</url></proceedings></r></dblpcites>
        <dblpcites><r><proceedings key='conf/fast/2025'><year>2025</year>
          <url>db/conf/fast/fast2025.html</url></proceedings></r></dblpcites>
        <dblpcites><r><proceedings key='conf/fast/2010sustainit'><year>2010</year>
          <url>db/conf/fast/sustainit2010.html</url></proceedings></r></dblpcites>
        </bht>"""

        def toc(year, key, title):
            return f"""<bht><dblpcites><r><inproceedings key='{key}' mdate='{year}-07-01'>
              <author pid='1'>Ada Lovelace</author><author pid='2'>Alan Turing</author>
              <title>{title}</title><year>{year}</year><booktitle>FAST</booktitle>
              <ee type='oa'>https://www.usenix.org/fast/{year}/{key.rsplit('/', 1)[-1]}</ee>
              <ee>https://doi.org/10.1234/{year}</ee>
              <crossref>conf/fast/{year}</crossref>
              <url>db/conf/fast/fast{year}.html#paper</url>
            </inproceedings></r></dblpcites></bht>""".encode()

        return {
            "/db/conf/fast/index.xml": index,
            "/db/conf/fast/fast2026.xml": toc(2026, "conf/fast/Test26", "SSD <i>Everywhere</i>."),
            "/db/conf/fast/fast2025.xml": toc(2025, "conf/fast/Test25", "Reliable NAND."),
            "/db/conf/fast/fast2024.xml": toc(2024, "conf/fast/Test24", "Storage Systems."),
        }

    def test_normalization(self):
        self.assertEqual(radar.normalize_title("  Flash-Translation Layer™  "), "flash translation layertm")
        self.assertEqual(
            radar.normalize_url("https://Example.com/a/?utm_source=x&keep=1#frag"),
            "https://example.com/a?keep=1",
        )
        self.assertEqual(radar.normalize_doi("https://doi.org/10.1/ABC"), "10.1/abc")

    def test_abstract_reconstruction(self):
        inverted = {"NAND": [0], "flash": [1], "retention": [3], "and": [2]}
        self.assertEqual(radar.reconstruct_abstract(inverted), "NAND flash and retention")

    def test_request_json_retries_a_truncated_body_in_the_same_run(self):
        truncated = b'{"results":[{"title":"cut'
        complete = b'{"results":[],"meta":{"next_cursor":null}}'
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[(truncated, {"X-Attempt": "1"}), (complete, {"X-Attempt": "2"})],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            payload, headers = radar.request_json(
                "https://api.example/works", retries=3
            )

        self.assertEqual(payload, {"results": [], "meta": {"next_cursor": None}})
        self.assertEqual(headers["X-Attempt"], "2")
        self.assertEqual(request.call_count, 2)
        first_headers = request.call_args_list[0].args[1]
        second_headers = request.call_args_list[1].args[1]
        self.assertNotIn("Cache-Control", first_headers)
        self.assertEqual(second_headers["Cache-Control"], "no-cache")
        sleep.assert_called_once_with(1)

    def test_request_json_reports_exhausted_integrity_retries(self):
        truncated = b'{"results":[{"title":"cut'
        with mock.patch.object(
            radar,
            "_request_once",
            return_value=(truncated, {}),
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                radar.InvalidJSONResponse,
                r"invalid, incomplete, or unusable JSON after 3 attempts .*JSONDecodeError",
            ):
                radar.request_json("https://api.example/works", retries=3)

        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[2] for call in request.call_args_list], [45, 45, 45])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_request_json_retries_a_socket_timeout_on_python_39(self):
        complete = b'{"results":[],"meta":{"next_cursor":null}}'
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[radar.socket.timeout("read timed out"), (complete, {})],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            payload, _ = radar.request_json("https://api.example/works", retries=3)

        self.assertEqual(payload["results"], [])
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_request_bytes_retries_a_socket_timeout_on_python_39(self):
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[radar.socket.timeout("read timed out"), (b"complete", {})],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            data, _ = radar.request_bytes("https://api.example/data", retries=3)

        self.assertEqual(data, b"complete")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_request_bytes_retries_a_short_content_length(self):
        def response(body, headers):
            value = mock.MagicMock()
            value.headers = headers
            value.read.return_value = body
            value.__enter__.return_value = value
            value.__exit__.return_value = False
            return value

        short = response(b'{"cut":', {"Content-Length": "100"})
        complete = response(b'{"ok":true}', {"Content-Length": "11"})
        with mock.patch.object(
            radar.urllib.request,
            "urlopen",
            side_effect=[short, complete],
        ) as urlopen, mock.patch.object(radar.time, "sleep") as sleep:
            data, _ = radar.request_bytes("https://api.example/data", retries=2)

        self.assertEqual(data, b'{"ok":true}')
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_request_bytes_does_not_sleep_after_final_retryable_http_error(self):
        errors = [
            radar.urllib.error.HTTPError(
                "https://api.example/data", 503, "unavailable", {}, None
            )
            for _ in range(2)
        ]
        with mock.patch.object(
            radar.urllib.request,
            "urlopen",
            side_effect=errors,
        ) as urlopen, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaises(radar.urllib.error.HTTPError):
                radar.request_bytes("https://api.example/data", retries=2)

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_request_bytes_aborts_a_long_rate_limit_without_sleeping(self):
        error = radar.urllib.error.HTTPError(
            "https://api.openalex.org/works",
            429,
            "rate limited",
            {"Retry-After": "3600"},
            None,
        )
        with mock.patch.object(
            radar.urllib.request,
            "urlopen",
            side_effect=error,
        ) as urlopen, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                RuntimeError, r"rate limited; retry after about 1h"
            ):
                radar.request_bytes("https://api.openalex.org/works", retries=5)

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_long_rate_limit_uses_an_accurate_provider_neutral_duration(self):
        error = radar.urllib.error.HTTPError(
            "https://api.example/data",
            429,
            "rate limited",
            {"Retry-After": "120"},
            None,
        )
        with self.assertRaisesRegex(
            RuntimeError, r"rate limited; retry after about 2m"
        ):
            radar._http_retry_delay(error, 0)

    def test_openalex_retries_valid_json_with_an_invalid_schema(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[
                (b'{"error":"temporary upstream response"}', {}),
                (b'{"results":[],"meta":{"count":0,"next_cursor":null}}', {}),
            ],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            self.assertEqual(radar.fetch_openalex(source, False, None), [])

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_openalex_retries_missing_and_empty_cursor_metadata(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[
                (b'{"results":[],"meta":{}}', {}),
                (b'{"results":[],"meta":{"next_cursor":""}}', {}),
                (b'{"results":[],"meta":{"count":0,"next_cursor":null}}', {}),
            ],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            self.assertEqual(radar.fetch_openalex(source, False, None), [])

        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_openalex_retries_empty_pages_with_a_continuation_cursor(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        responses = [
            ({"results": [], "meta": {"count": 1, "next_cursor": "more"}}, {}),
            ({"results": [], "meta": {"count": 0, "next_cursor": None}}, {}),
        ]
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[(json.dumps(body).encode(), headers) for body, headers in responses],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            self.assertEqual(radar.fetch_openalex(source, False, None), [])

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_openalex_retries_missing_id_and_invalid_nested_work_shape(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        missing_id = {
            "title": "SSD reliability",
            "authorships": [],
        }
        invalid_authorships = {
            "id": "https://openalex.org/W1",
            "title": "SSD reliability",
            "authorships": "not-an-array",
        }
        responses = [
            ({"results": [missing_id], "meta": {"count": 1, "next_cursor": "more"}}, {}),
            ({"results": [invalid_authorships], "meta": {"count": 1, "next_cursor": "more"}}, {}),
            ({"results": [], "meta": {"count": 0, "next_cursor": None}}, {}),
        ]
        with mock.patch.object(
            radar,
            "_request_once",
            side_effect=[(json.dumps(body).encode(), headers) for body, headers in responses],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            self.assertEqual(radar.fetch_openalex(source, False, None), [])

        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_openalex_accepts_a_stable_id_with_nullable_optional_fields(self):
        radar.validate_openalex_payload(
            {
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": None,
                        "title": None,
                        "publication_date": None,
                        "type": None,
                        "authorships": None,
                        "primary_location": None,
                        "best_oa_location": None,
                        "abstract_inverted_index": None,
                    }
                ],
                "meta": {"count": 1, "next_cursor": "more"},
            }
        )

    def test_openalex_stops_after_the_schema_retry_budget(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        invalid = b'{"error":"temporary upstream response"}'
        with mock.patch.object(
            radar,
            "_request_once",
            return_value=(invalid, {}),
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                radar.InvalidJSONResponse,
                r"invalid, incomplete, or unusable JSON after 5 attempts",
            ):
                radar.fetch_openalex(source, False, None)

        self.assertEqual(request.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8])

    def test_openalex_rejects_a_repeated_cursor_without_partial_success(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "SSD",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {},
                }
            ],
            "meta": {"count": 1, "next_cursor": "*"},
        }
        with mock.patch.object(
            radar, "request_json", return_value=(payload, {})
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "repeated cursor"):
                radar.fetch_openalex(source, False, None)

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["per-page"], ["50"])

    def test_openalex_restarts_a_query_when_the_final_count_is_incomplete(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
        }
        partial = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "SSD reliability",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {},
                }
            ],
            "meta": {"count": 2, "next_cursor": None},
        }
        complete_empty = {
            "results": [],
            "meta": {"count": 0, "next_cursor": None},
        }
        with mock.patch.object(
            radar,
            "request_json",
            side_effect=[(partial, {}), (complete_empty, {})],
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            self.assertEqual(radar.fetch_openalex(source, False, None), [])

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(
            request.call_args_list[1].kwargs["headers"],
            {"Cache-Control": "no-cache"},
        )

    def test_openalex_rejects_a_deterministically_oversized_query_immediately(self):
        source = {
            "id": "openalex_ssd",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
            "openalex_per_page": 50,
            "openalex_max_pages": 2,
        }
        first_page = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "SSD reliability",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {},
                }
            ],
            "meta": {"count": 101, "next_cursor": "page-2"},
        }
        with mock.patch.object(
            radar,
            "request_json",
            return_value=(first_page, {}),
        ) as request, mock.patch.object(radar.time, "sleep") as sleep:
            with self.assertRaisesRegex(
                radar.OpenAlexPageLimitExceeded,
                r"101 results.*100-result safety bound",
            ):
                radar.fetch_openalex(source, False, None)

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_openalex_second_page_failure_never_ingests_a_partial_snapshot(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "openalex_ssd",
            "name": "OpenAlex",
            "kind": "openalex",
            "category": "paper",
            "homepage": "https://openalex.org/",
            "endpoint": "https://api.openalex.org/works",
            "queries": [{"search": "solid state drive"}],
            "incremental_overlap_days": 365,
            "enabled": True,
        }
        radar.register_sources(conn, {"sources": [source]})
        last_success = "2026-07-20T00:00:00Z"
        conn.execute(
            "UPDATE sources SET initialized=1,last_success_at=?,last_full_at=? WHERE id=?",
            (last_success, last_success, source["id"]),
        )
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','running')"
        ).lastrowid
        first_page = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "SSD reliability",
                    "type": "article",
                    "authorships": [],
                    "primary_location": {},
                }
            ],
            "meta": {"count": 2, "next_cursor": "page-2"},
        }
        page_error = radar.InvalidJSONResponse("page 2 remained incomplete")

        with mock.patch.object(
            radar,
            "request_json",
            side_effect=[(first_page, {}), page_error],
        ):
            with self.assertRaises(radar.InvalidJSONResponse):
                radar.sync_source(conn, run_id, source, False)

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0], 0
        )
        radar.record_source_failure(conn, source["id"], page_error)
        state = conn.execute(
            "SELECT last_success_at,last_error,failure_count FROM sources WHERE id=?",
            (source["id"],),
        ).fetchone()
        self.assertEqual(state["last_success_at"], last_success)
        self.assertIn("page 2 remained incomplete", state["last_error"])
        self.assertEqual(state["failure_count"], 1)
        conn.close()

    def test_rss_parsing(self):
        source = {"id": "ocp_storage", "name": "OCP Storage"}
        feed = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <item><title>SSD update</title><link>https://example.com/x</link>
        <guid>x-1</guid><pubDate>Sun, 19 Jul 2026 00:00:00 +0000</pubDate>
        <description><![CDATA[<p>NVMe FDP news</p>]]></description></item>
        </channel></rss>"""
        rows = radar.parse_feed(feed, source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "x-1")
        self.assertEqual(rows[0]["summary"], "NVMe FDP news")
        self.assertEqual(rows[0]["item_type"], "message")

    def test_rejects_xml_entities(self):
        source = {"id": "x", "name": "X"}
        with self.assertRaises(ValueError):
            radar.parse_feed(b"<!DOCTYPE x [<!ENTITY y 'z'>]><rss/>", source)

    def test_page_fetch_follows_configured_same_site_program_link(self):
        homepage = b"""<html><main>NVMW announcements</main>
        <a href='/program-3/'>Program (2026)</a>
        <a href='https://example.com/program'>External program</a></html>"""
        program = b"<html><main><h1>PROGRAM</h1><h3>Session 3: SSD</h3></main></html>"
        calls = []

        def fake_request(url, *_args, **_kwargs):
            calls.append(url)
            if url == "https://nvmw.ucsd.edu/":
                return homepage, {}
            if url == "https://nvmw.ucsd.edu/program-3":
                return program, {}
            raise AssertionError(f"unexpected URL: {url}")

        source = {
            "id": "nvmw_official",
            "name": "NVMW Official",
            "endpoint": "https://nvmw.ucsd.edu/",
            "follow_link_patterns": ["program"],
            "max_followed_pages": 2,
        }
        with mock.patch.object(radar, "request_bytes", side_effect=fake_request):
            rows = radar.fetch_page(source, False, None)

        self.assertEqual(calls, ["https://nvmw.ucsd.edu/", "https://nvmw.ucsd.edu/program-3"])
        self.assertIn("NVMW announcements", rows[0]["summary"])
        self.assertIn("Session 3: SSD", rows[0]["summary"])
        self.assertEqual(
            set(rows[0]["raw"]["pages"]),
            {"https://nvmw.ucsd.edu/", "https://nvmw.ucsd.edu/program-3"},
        )
        changed = dict(rows[0])
        changed["content_fingerprint"] = "changed-linked-page"
        self.assertNotEqual(radar.content_hash(rows[0]), radar.content_hash(changed))

    def test_fetch_configuration_change_bypasses_stale_poll_interval(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "nvmw_official",
            "name": "NVMW Official",
            "kind": "page",
            "category": "NVMW",
            "homepage": "https://nvmw.ucsd.edu/",
            "endpoint": "https://nvmw.ucsd.edu/",
            "minimum_interval_hours": 6,
            "enabled": True,
        }
        radar.register_sources(conn, {"sources": [source]})
        conn.execute(
            "UPDATE sources SET initialized=1,last_success_at='2026-07-19T00:00:00Z' WHERE id=?",
            (source["id"],),
        )
        conn.commit()

        radar.register_sources(conn, {"sources": [source]})
        self.assertEqual(
            conn.execute("SELECT last_success_at FROM sources WHERE id=?", (source["id"],)).fetchone()[0],
            "2026-07-19T00:00:00Z",
        )

        changed = dict(source, follow_link_patterns=["program"])
        radar.register_sources(conn, {"sources": [changed]})
        row = conn.execute(
            "SELECT initialized,last_success_at FROM sources WHERE id=?", (source["id"],)
        ).fetchone()
        self.assertEqual(row["initialized"], 1)
        self.assertIsNone(row["last_success_at"])
        conn.close()

    def test_recent_failure_is_retried_instead_of_being_cleared_by_freshness_skip(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "academic_source",
            "name": "Academic source",
            "kind": "rss",
            "category": "paper",
            "homepage": "https://example.com/",
            "endpoint": "https://example.com/feed.xml",
            "minimum_interval_hours": 24,
            "enabled": True,
        }
        radar.register_sources(conn, {"sources": [source]})
        recent = radar.iso(radar.utcnow() - radar.dt.timedelta(minutes=5))
        conn.execute(
            "UPDATE sources SET initialized=1,last_success_at=?,last_attempt_at=?,"
            "last_error='timeout',failure_count=1 WHERE id=?",
            (recent, recent, source["id"]),
        )
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','running')"
        ).lastrowid
        fetcher = mock.Mock(return_value=[])

        with mock.patch.dict(radar.FETCHERS, {"rss": fetcher}):
            result = radar.sync_source(conn, run_id, source, False)

        fetcher.assert_called_once()
        self.assertNotIn("skipped", result)
        state = conn.execute(
            "SELECT last_error,failure_count FROM sources WHERE id=?", (source["id"],)
        ).fetchone()
        self.assertIsNone(state["last_error"])
        self.assertEqual(state["failure_count"], 0)
        conn.close()

    def test_manual_repair_run_bypasses_a_fresh_minimum_interval(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "academic_source",
            "name": "Academic source",
            "kind": "rss",
            "category": "paper",
            "homepage": "https://example.com/",
            "endpoint": "https://example.com/feed.xml",
            "minimum_interval_hours": 24,
            "enabled": True,
        }
        radar.register_sources(conn, {"sources": [source]})
        recent = radar.iso(radar.utcnow() - radar.dt.timedelta(minutes=5))
        conn.execute(
            "UPDATE sources SET initialized=1,last_success_at=? WHERE id=?",
            (recent, source["id"]),
        )
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','running')"
        ).lastrowid
        fetcher = mock.Mock(return_value=[])

        with mock.patch.dict(
            os.environ, {"RADAR_BYPASS_MINIMUM_INTERVAL": "1"}
        ), mock.patch.dict(radar.FETCHERS, {"rss": fetcher}):
            result = radar.sync_source(conn, run_id, source, False)

        fetcher.assert_called_once()
        self.assertNotIn("skipped", result)
        conn.close()

    def test_fetch_configuration_can_rebaseline_a_source_without_notification_flood(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "nvmw_official", "name": "NVMW", "kind": "page", "category": "NVMW",
            "homepage": "https://nvmw.example/", "endpoint": "https://nvmw.example/",
        }
        radar.register_sources(conn, {"sources": [source]})
        conn.execute("UPDATE sources SET initialized=1,last_success_at='2026-01-01T00:00:00Z'")
        conn.commit()
        changed = dict(
            source,
            kind="nvmw",
            endpoint="https://nvmw.example/wp-json/wp/v2/pages/1",
            rebaseline_revision=1,
        )
        radar.register_sources(conn, {"sources": [changed]})
        row = conn.execute("SELECT initialized,last_success_at FROM sources").fetchone()
        self.assertEqual(row["initialized"], 0)
        self.assertIsNone(row["last_success_at"])
        conn.close()

    def test_nvmw_program_is_split_into_independent_talks_with_abstracts(self):
        content = """
        <div class='nmw-popup' id='paper-7' style='display:none'>
          <h3>Flash Translation at Scale</h3>
          <p><span class='nvmw-author-list'>A. Engineer (Lab);</span></p>
          <p><b>Abstract:</b> We reduce garbage-collection interference with host writes.</p>
          <a href='/nvmw2026/final7.pdf'>Extended Abstract</a>
        </div>
        <div class='nmw-popup' id='paper-8'><h3>NAND Reliability</h3>
          <p><b>Abstract:</b> We characterize retention and read disturb.</p></div>
        """
        rows = radar.parse_nvmw_program(content, "https://nvmw.example/program/")
        self.assertEqual([row["external_id"] for row in rows], ["paper-7", "paper-8"])
        self.assertEqual(rows[0]["title"], "Flash Translation at Scale")
        self.assertEqual(rows[0]["authors"], "A. Engineer (Lab)")
        self.assertIn("garbage-collection", rows[0]["summary"])
        self.assertEqual(rows[0]["raw"]["links"], ["https://nvmw.example/nvmw2026/final7.pdf"])
        self.assertNotEqual(rows[0]["content_fingerprint"], rows[1]["content_fingerprint"])

    def test_nvmw_discovers_current_program_and_uses_stable_publish_date(self):
        homepage = b"<a href='/program-4/'>Program (2027)</a>"
        page = [{
            "id": 5000,
            "date_gmt": "2027-02-01 08:00:00",
            "modified_gmt": "2027-07-19 12:00:00",
            "link": "https://nvmw.ucsd.edu/program-4/",
            "title": {"rendered": "Program NVMW 2027"},
            "content": {"rendered": """
                <div class='nmw-popup' id='paper-1'><h3>Future SSD</h3>
                <p><b>Abstract:</b> A source-backed abstract.</p></div>
            """},
        }]
        calls = []

        def fake_json(url, *_args, **_kwargs):
            calls.append(url)
            return page, {}

        source = {
            "id": "nvmw_official", "name": "NVMW", "kind": "nvmw",
            "endpoint": "https://nvmw.ucsd.edu/",
            "follow_link_patterns": ["program"], "max_followed_pages": 1,
        }
        with mock.patch.object(radar, "request_bytes", return_value=(homepage, {})), mock.patch.object(
            radar, "request_json", side_effect=fake_json
        ):
            rows = radar.fetch_nvmw(source, False, None)

        self.assertEqual(len(rows), 1)
        self.assertIn("slug=program-4", calls[0])
        self.assertEqual(rows[0]["external_id"], "5000:2027:paper-1")
        self.assertEqual(rows[0]["published_at"], "2027-02-01T08:00:00Z")
        self.assertEqual(rows[0]["url"], "https://nvmw.ucsd.edu/program-4#paper-1")

    def test_nvmw_ingest_preserves_talk_fragment(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
            ("nvmw_official", "NVMW", "nvmw", "NVMW", "https://nvmw.example", "https://nvmw.example", "{}"),
        )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','ok')").lastrowid
        record = {
            "external_id": "5000:2027:paper-1", "item_type": "paper", "title": "Future SSD",
            "url": "https://nvmw.example/program/#paper-1", "summary": "NAND abstract",
            "authors": "A", "venue": "NVMW 2027", "published_at": "2027-02-01",
        }
        radar.ingest_record(
            conn, run_id,
            {"id": "nvmw_official", "kind": "nvmw", "category": "NVMW"},
            record, True,
        )
        self.assertEqual(
            conn.execute("SELECT url FROM items").fetchone()[0],
            "https://nvmw.example/program#paper-1",
        )
        conn.close()

    def test_nvme_specifications_are_split_and_track_current_file(self):
        payload = {
            "posts": [{
                "id": 42,
                "post_title": "NVM Express Base Specification",
                "post_content": "Defines the register interface and command architecture.",
                "post_modified_gmt": "2026-07-01 10:00:00",
                "href": "https://nvmexpress.example/specification/base/",
                "file": {"id": 99, "title": "NVMe Base 2.3", "url": "https://nvmexpress.example/base-2.3.pdf"},
                "type": [{"name": "Base"}],
            }]
        }
        with mock.patch.object(radar, "request_json", return_value=(payload, {})):
            rows = radar.fetch_nvme_specifications(
                {"endpoint": "https://nvmexpress.example/api", "name": "NVMe specs"}, False, None
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_id"], "42")
        self.assertIn("NVMe Base 2.3", rows[0]["summary"])
        self.assertEqual(rows[0]["raw"]["file_id"], 99)
        self.assertEqual(rows[0]["item_type"], "standard")

    def test_page_fingerprint_ignores_dynamic_markup_ids(self):
        source = {
            "id": "nvmw_official",
            "name": "NVMW Official",
            "endpoint": "https://nvmw.ucsd.edu/",
        }
        variants = [
            b"<main><div id='ult_btn_2061355577'>Same announcement</div></main>",
            b"<main><div id='ult_btn_560431465'>Same announcement</div></main>",
        ]
        with mock.patch.object(
            radar, "request_bytes", side_effect=[(variants[0], {}), (variants[1], {})]
        ):
            first = radar.fetch_page(source, False, None)[0]
            second = radar.fetch_page(source, False, None)[0]

        self.assertEqual(first["content_fingerprint"], second["content_fingerprint"])
        self.assertEqual(radar.content_hash(first), radar.content_hash(second))

    def test_page_fingerprint_detects_linked_text_beyond_summary_limit(self):
        source = {
            "id": "nvmw_official",
            "name": "NVMW Official",
            "endpoint": "https://nvmw.ucsd.edu/",
            "follow_link_patterns": ["program"],
        }
        homepage = b"<main>NVMW</main><a href='/program-3/'>Program</a>"

        def variant(tail):
            program = ("<main>" + ("X" * 35000) + tail + "</main>").encode()

            def fake_request(url, *_args, **_kwargs):
                return (homepage, {}) if url.endswith(".edu/") else (program, {})

            with mock.patch.object(radar, "request_bytes", side_effect=fake_request):
                return radar.fetch_page(source, False, None)[0]

        first = variant("TAIL-A")
        second = variant("TAIL-B")
        self.assertEqual(first["summary"], second["summary"])
        self.assertNotEqual(first["content_fingerprint"], second["content_fingerprint"])
        self.assertNotEqual(radar.content_hash(first), radar.content_hash(second))

    def test_dblp_incremental_uses_latest_two_tocs_and_mirror_fallback(self):
        responses = self.dblp_fixture_responses()
        calls = []

        def fake_request(url, *_args, **_kwargs):
            calls.append(url)
            parsed = radar.urllib.parse.urlsplit(url)
            if parsed.netloc == "dblp.org":
                raise radar.urllib.error.URLError("remote disconnected")
            return responses[parsed.path], {}

        source = {
            "endpoint": "https://dblp.org/db/conf/fast/index.xml",
            "mirrors": ["https://dblp.org/", "https://dblp.dagstuhl.de/", "https://dblp.uni-trier.de/"],
        }
        with mock.patch.object(radar, "request_bytes", side_effect=fake_request), mock.patch.object(
            radar.time, "sleep"
        ) as sleep:
            rows = radar.fetch_dblp(source, False, None)

        self.assertEqual([row["external_id"] for row in rows], ["conf/fast/Test26", "conf/fast/Test25"])
        self.assertEqual(rows[0]["title"], "SSD Everywhere.")
        self.assertEqual(rows[0]["authors"], "Ada Lovelace, Alan Turing")
        self.assertEqual(rows[0]["venue"], "FAST")
        self.assertEqual(rows[0]["published_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(rows[0]["doi"], "https://doi.org/10.1234/2026")
        self.assertTrue(any(url.startswith("https://dblp.dagstuhl.de/") for url in calls))
        self.assertEqual(sum(url.startswith("https://dblp.org/") for url in calls), 1)
        self.assertFalse(any("fast2024.xml" in url for url in calls))
        sleep.assert_has_calls([mock.call(1.1), mock.call(1.1)])

    def test_dblp_full_fetches_all_main_tocs_but_not_workshops(self):
        responses = self.dblp_fixture_responses()
        paths = []

        def fake_request(url, *_args, **_kwargs):
            path = radar.urllib.parse.urlsplit(url).path
            paths.append(path)
            return responses[path], {}

        source = {
            "endpoint": "https://dblp.org/db/conf/fast/index.xml",
            "mirrors": ["https://dblp.org/"],
        }
        with mock.patch.object(radar, "request_bytes", side_effect=fake_request), mock.patch.object(
            radar.time, "sleep"
        ):
            rows = radar.fetch_dblp(source, True, "2000-01-01")

        self.assertEqual(len(rows), 3)
        self.assertIn("/db/conf/fast/fast2024.xml", paths)
        self.assertNotIn("/db/conf/fast/sustainit2010.xml", paths)

    def test_dblp_gets_periodic_full_scan_after_thirty_days(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        old = radar.iso(radar.utcnow() - radar.dt.timedelta(days=31))
        conn.execute(
            """
            INSERT INTO sources(
                id,name,kind,category,homepage,endpoint,config_json,initialized,
                last_success_at,last_full_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "fast_dblp", "FAST", "dblp", "system", "https://dblp.org/",
                "https://dblp.org/db/conf/fast/index.xml", "{}", 1, old, old,
            ),
        )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','running')").lastrowid
        full_values = []

        def fake_fetch(_source, full, _since):
            full_values.append(full)
            return []

        source = {
            "id": "fast_dblp", "name": "FAST", "kind": "dblp", "category": "system",
            "homepage": "https://dblp.org/", "endpoint": "https://dblp.org/db/conf/fast/index.xml",
        }
        with mock.patch.dict(radar.FETCHERS, {"dblp": fake_fetch}):
            radar.sync_source(conn, run_id, source, False)
        self.assertEqual(full_values, [True])
        self.assertGreater(conn.execute("SELECT last_full_at FROM sources").fetchone()[0], old)
        conn.close()

    def test_doi_dedupes_across_sources(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        for source_id in ("a", "b"):
            conn.execute(
                "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
                (source_id, source_id, "rss", "paper", "https://example.com", "https://example.com/feed", "{}"),
            )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','running')").lastrowid
        base = {
            "external_id": "one", "item_type": "paper", "title": "A NAND Paper",
            "url": "https://example.com/one", "doi": "10.1234/SAME", "authors": "A. Author",
            "venue": "FAST", "published_at": "2026-01-01", "summary": "NAND retention",
        }
        radar.ingest_record(conn, run_id, {"id": "a", "category": "paper"}, dict(base), True)
        second = dict(
            base,
            external_id="two",
            url="https://example.org/two",
            title="A NAND Paper: camera-ready version",
            published_at="2025-12-31",
        )
        radar.ingest_record(conn, run_id, {"id": "b", "category": "paper"}, second, True)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_events WHERE event_type='new'").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT original_published_at FROM items").fetchone()[0],
            "2025-12-31T00:00:00Z",
        )

    def test_different_dois_do_not_merge_same_title_and_year(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        for source_id in ("a", "b"):
            conn.execute(
                "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
                (source_id, source_id, "rss", "paper", "https://example.com", "https://example.com/feed", "{}"),
            )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','running')").lastrowid
        base = {
            "external_id": "one", "item_type": "paper", "title": "Shared Title",
            "url": "https://example.com/one", "doi": "10.1234/one", "authors": "A",
            "venue": "V", "published_at": "2026-01-01", "summary": "NAND",
        }
        radar.ingest_record(conn, run_id, {"id": "a", "category": "paper"}, dict(base), True)
        radar.ingest_record(
            conn, run_id, {"id": "b", "category": "paper"},
            dict(base, external_id="two", doi="10.1234/two"), True,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_pending_events_survive_across_runs_until_delivered(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
            ("a", "A", "rss", "paper", "https://example.com", "https://example.com/feed", "{}"),
        )
        first_run = conn.execute("INSERT INTO runs(started_at,status) VALUES('one','abandoned')").lastrowid
        record = {
            "external_id": "one", "item_type": "paper", "title": "Pending NAND Paper",
            "url": "https://example.com/one", "doi": "10.1234/pending", "authors": "A",
            "venue": "V", "published_at": "2026-01-01", "summary": "NAND",
        }
        radar.ingest_record(conn, first_run, {"id": "a", "category": "paper"}, record, True)
        radar.briefs.ensure_fallback_briefs(conn)
        mark_professional(conn)
        second_run = conn.execute("INSERT INTO runs(started_at,status) VALUES('two','running')").lastrowid
        payload = radar.report_payload(conn, second_run, [])
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(payload["run_detected_new_count"], 0)
        self.assertEqual(payload["run_detected_updated_count"], 0)
        self.assertEqual(payload["total_item_count"], 1)
        self.assertEqual(payload["professional_brief_count"], 1)
        self.assertEqual(payload["pending_history_brief_count"], 0)
        self.assertEqual(payload["retry_brief_count"], 0)
        self.assertEqual(payload["backfill_percent"], 100.0)
        conn.execute("UPDATE run_events SET delivered_at='now' WHERE delivered_at IS NULL")
        self.assertEqual(radar.report_payload(conn, second_run, [])["new_count"], 0)

    def test_candidate_detection_is_distinct_from_deliverable_professional_events(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        source = {
            "id": "academic_source",
            "name": "Academic source",
            "kind": "rss",
            "category": "paper",
            "homepage": "https://example.com/",
            "endpoint": "https://example.com/feed.xml",
            "enabled": True,
        }
        radar.register_sources(conn, {"sources": [source]})
        conn.execute(
            "UPDATE sources SET initialized=1,last_success_at='2026-07-20T00:00:00Z' "
            "WHERE id=?",
            (source["id"],),
        )
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','running')"
        ).lastrowid
        record = {
            "external_id": "paper-1",
            "item_type": "paper",
            "title": "A new NAND SSD reliability paper",
            "url": "https://example.com/paper-1",
            "doi": "10.1234/new-paper",
            "authors": "A. Author",
            "venue": "FAST",
            "published_at": "2026-07-21",
            "summary": "NAND flash retention and garbage collection.",
        }
        with mock.patch.dict(radar.FETCHERS, {"rss": mock.Mock(return_value=[record])}):
            result = radar.sync_source(conn, run_id, source, False)
        radar.briefs.ensure_fallback_briefs(conn)
        payload = radar.report_payload(conn, run_id, [result])

        self.assertEqual(payload["run_detected_new_count"], 1)
        self.assertEqual(payload["new_count"], 0)
        self.assertEqual(payload["awaiting_brief_count"], 1)
        conn.close()

    def test_retention_hides_old_history_without_losing_current_old_item_update(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) "
            "VALUES('a','A','rss','paper','https://example.com','https://example.com/feed','{}',1)"
        )
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('2026-07-20T00:00:00Z','ok')"
        ).lastrowid

        def add_item(key, published):
            item_id = conn.execute(
                """
                INSERT INTO items(
                    canonical_key,item_type,title,normalized_title,url,published_at,
                    original_published_at,summary,topics_json,discovered_at,updated_at,baseline
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    key,
                    "paper",
                    key,
                    key,
                    f"https://example.com/{key}",
                    published,
                    published,
                    "NAND evidence.",
                    "[]",
                    "2026-07-20T00:00:00Z",
                    "2026-07-20T00:00:00Z",
                ),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO item_sources(
                    source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at
                ) VALUES('a',?,?,?,'hash','now','now')
                """,
                (key, item_id, f"https://example.com/{key}"),
            )
            return item_id

        old = add_item("old", "2019-01-01T00:00:00Z")
        recent = add_item("recent", "2026-01-01T00:00:00Z")
        stale = add_item("stale", "2026-01-01T00:00:00Z")
        conn.executemany(
            "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,'a',?,?)",
            (
                (run_id, old, "new", "2026-07-20T01:00:00Z"),
                (run_id, old, "updated", "2026-07-20T02:00:00Z"),
                (run_id, recent, "new", "2026-07-20T03:00:00Z"),
                (run_id, stale, "updated", "2021-07-19T23:59:59Z"),
            ),
        )
        conn.commit()
        radar.briefs.ensure_fallback_briefs(conn)
        mark_professional(conn)

        self.assertEqual(radar.suppress_out_of_scope_events(conn, "2021-07-20"), 2)
        self.assertEqual(
            [row["canonical_key"] for row in radar.item_rows(conn, "2021-07-20")],
            ["stale", "recent"],
        )
        self.assertEqual(
            [row["canonical_key"] for row in radar.pending_event_rows(conn, "new", "2021-07-20")],
            ["recent"],
        )
        self.assertEqual(
            [row["canonical_key"] for row in radar.pending_event_rows(conn, "updated", "2021-07-20")],
            ["old"],
        )
        payload = radar.report_payload(
            conn, run_id, [], history_cutoff="2021-07-20", history_window_years=5
        )
        self.assertEqual((payload["new_count"], payload["updated_count"]), (1, 1))
        self.assertEqual(payload["total_item_count"], 2)
        self.assertEqual(payload["history_cutoff"], "2021-07-20")
        conn.close()

    def test_late_old_discovery_is_not_new_but_later_material_change_is_an_update(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) "
            "VALUES('a','A','rss','paper','https://example.com','https://example.com/feed','{}',1)"
        )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','ok')").lastrowid
        source = {"id": "a", "category": "paper"}
        record = {
            "external_id": "old",
            "item_type": "paper",
            "title": "Old NAND paper",
            "url": "https://example.com/old",
            "doi": "10.1/old",
            "authors": "A",
            "venue": "FAST",
            "published_at": "2019-01-01",
            "summary": "Original NAND evidence.",
        }
        self.assertEqual(
            radar.ingest_record(
                conn, run_id, source, dict(record), True, history_cutoff="2021-07-20"
            ),
            (False, False),
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT baseline FROM items").fetchone()[0], 1)

        changed = dict(record, summary="Materially revised NAND evidence.")
        self.assertEqual(
            radar.ingest_record(
                conn, run_id, source, changed, True, history_cutoff="2021-07-20"
            ),
            (False, True),
        )
        event = conn.execute("SELECT event_type FROM run_events").fetchone()
        self.assertEqual(event["event_type"], "updated")
        conn.close()

    def test_brief_generation_failure_is_reported_but_does_not_fail_source_health(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        radar.briefs.ensure_schema(conn)
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','ok')"
        ).lastrowid
        payload = radar.report_payload(
            conn,
            run_id,
            [],
            brief_failures=[
                {
                    "id": radar.BRIEF_GENERATION_FAILURE_ID,
                    "name": "专业简报生成",
                    "ok": False,
                    "failed_count": 2,
                    "error": "证据校验失败；下次自动重试",
                }
            ],
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_failure_count"], 0)
        self.assertFalse(payload["brief_generation_ok"])
        self.assertEqual(payload["brief_generation_failure_count"], 2)
        self.assertEqual(payload["source_failures"], [])
        self.assertEqual(len(payload["brief_failures"]), 1)
        self.assertEqual(len(payload["failures"]), 1)
        conn.close()

    def test_real_source_failure_still_fails_source_health(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        radar.briefs.ensure_schema(conn)
        run_id = conn.execute(
            "INSERT INTO runs(started_at,status) VALUES('now','partial')"
        ).lastrowid
        payload = radar.report_payload(
            conn,
            run_id,
            [
                {
                    "id": "fast_dblp",
                    "name": "FAST",
                    "ok": True,
                    "fetched": 10,
                    "new": 2,
                    "updated": 1,
                },
                {
                    "id": "nvmw_official",
                    "name": "NVMW",
                    "ok": True,
                    "skipped": True,
                    "fetched": 0,
                    "new": 0,
                    "updated": 0,
                },
                {
                    "id": "openalex_ssd",
                    "name": "OpenAlex",
                    "ok": False,
                    "error": "HTTP 503",
                }
            ],
            brief_failures=[
                {
                    "id": radar.BRIEF_GENERATION_FAILURE_ID,
                    "name": "专业简报生成",
                    "ok": False,
                    "failed_count": 3,
                    "error": "模型限流；下次自动重试",
                },
            ],
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_evaluated_count"], 3)
        self.assertEqual(payload["source_attempted_count"], 2)
        self.assertEqual(payload["source_success_count"], 1)
        self.assertEqual(payload["source_skipped_count"], 1)
        self.assertEqual(payload["source_failure_count"], 1)
        self.assertEqual(payload["run_detected_new_count"], 2)
        self.assertEqual(payload["run_detected_updated_count"], 1)
        self.assertEqual(
            [check["status"] for check in payload["source_checks"]],
            ["success", "fresh_cached", "failed"],
        )
        self.assertEqual(len(payload["source_failures"]), 1)
        self.assertFalse(payload["brief_generation_ok"])
        self.assertEqual(payload["brief_generation_failure_count"], 3)
        self.assertEqual(len(payload["brief_failures"]), 1)
        self.assertEqual(len(payload["failures"]), 2)
        conn.close()

    def test_material_update_is_versioned_and_enters_rss(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
            ("a", "A", "rss", "paper", "https://example.com", "https://example.com/feed", "{}"),
        )
        first_run = conn.execute("INSERT INTO runs(started_at,status) VALUES('one','ok')").lastrowid
        record = {
            "external_id": "one", "item_type": "paper", "title": "Versioned NAND Paper",
            "url": "https://example.com/one", "doi": "10.1234/versioned", "authors": "A",
            "venue": "V", "published_at": "2026-01-01", "summary": "Old NAND summary",
        }
        radar.ingest_record(conn, first_run, {"id": "a", "category": "paper"}, dict(record), True)
        conn.execute("UPDATE run_events SET delivered_at='done'")
        second_run = conn.execute("INSERT INTO runs(started_at,status) VALUES('two','ok')").lastrowid
        radar.ingest_record(
            conn, second_run, {"id": "a", "category": "paper"},
            dict(record, summary="New NAND summary with corrected findings"), True,
        )
        self.assertEqual(conn.execute("SELECT summary FROM items").fetchone()[0], "New NAND summary with corrected findings")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0], 2)
        radar.briefs.ensure_fallback_briefs(conn)
        item_id = conn.execute("SELECT id FROM items").fetchone()[0]
        mark_professional(conn, item_id)
        rows = conn.execute(
            "SELECT i.*,1 AS retention_eligible,b.public_id,b.status AS brief_status,b.brief_json,"
            "e.run_id,e.event_type,e.created_at AS event_created_at "
            "FROM run_events e JOIN items i ON i.id=e.item_id "
            "JOIN item_briefs b ON b.item_id=i.id WHERE e.event_type='updated'"
        ).fetchall()
        feed = radar.rss_xml(rows)
        self.assertIn("[更新] 中文：Versioned NAND Paper", feed)
        self.assertIn(":updated", feed)

    def test_delivered_baseline_update_is_withheld_and_requeued_until_professional(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES('a','A','rss','paper','https://example.com','https://example.com/feed','{}',1)"
        )
        item_id = conn.execute(
            """
            INSERT INTO items(canonical_key,item_type,title,normalized_title,url,summary,
                              topics_json,discovered_at,updated_at,baseline)
            VALUES('legacy','paper','Legacy update','legacy update','https://example.com/x',
                   'Updated NAND evidence','[]','2026-01-01T00:00:00Z','2026-07-19T00:00:00Z',1)
            """
        ).lastrowid
        conn.execute(
            "INSERT INTO item_sources(source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at) VALUES('a','x',?,'https://example.com/x','hash','now','now')",
            (item_id,),
        )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','ok')").lastrowid
        conn.execute(
            "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at,delivered_at) VALUES(?,?,'a','updated','now','legacy-delivered')",
            (run_id, item_id),
        )
        conn.commit()
        radar.briefs.ensure_fallback_briefs(conn)
        self.assertEqual(
            sum(len(chunk) for _, _, chunk in radar.archive_feed_specs(radar.item_rows(conn))),
            0,
        )
        self.assertEqual(radar.requeue_unprofessional_events(conn), 1)
        self.assertIsNone(conn.execute("SELECT delivered_at FROM run_events").fetchone()[0])
        mark_professional(conn, item_id)
        self.assertEqual(
            sum(len(chunk) for _, _, chunk in radar.archive_feed_specs(radar.item_rows(conn))),
            1,
        )
        conn.close()

    def test_public_base_url_normalization(self):
        self.assertEqual(
            radar.normalize_public_base_url("https://Example.com/radar?ignored=1#x"),
            "https://Example.com/radar/",
        )
        self.assertEqual(
            radar.public_site_url("live.xml", "https://example.com/radar"),
            "https://example.com/radar/live.xml",
        )
        with self.assertRaises(ValueError):
            radar.normalize_public_base_url("/not-public")

    def test_builds_netnewswire_live_alias_and_chunked_archives(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized,item_count) "
            "VALUES(?,?,?,?,?,?,?,1,352)",
            ("a", "A", "rss", "paper", "https://source.example", "https://source.example/feed", "{}"),
        )
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('2026-07-19','ok')").lastrowid
        for number in range(352):
            is_live = number == 351
            title = "Future live item" if is_live else f"Baseline item {number:03d}"
            item_id = conn.execute(
                """
                INSERT INTO items(
                    canonical_key,item_type,title,normalized_title,url,authors,venue,published_at,
                    summary,topics_json,discovered_at,updated_at,baseline
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"item:{number}", "paper", title, radar.normalize_title(title),
                    f"https://papers.example/{number}", "Author", "FAST",
                    f"2026-01-{number % 28 + 1:02d}T00:00:00Z", "NAND", '["NAND 可靠性"]',
                    "2026-07-19T00:00:00Z", "2026-07-19T00:00:00Z", 0 if is_live else 1,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO item_sources(source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("a", str(number), item_id, f"https://papers.example/{number}", str(number), "now", "now"),
            )
            if is_live:
                conn.execute(
                    "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,?,?,?)",
                    (run_id, item_id, "a", "new", "2026-07-19T12:00:00Z"),
                )
        conn.commit()
        radar.briefs.ensure_fallback_briefs(conn)
        mark_professional(conn, item_id)
        pending_id = conn.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,normalized_title,url,published_at,summary,
                topics_json,discovered_at,updated_at,baseline
            ) VALUES('item:pending','paper','Pending raw item','pending raw item',
                     'https://papers.example/pending','2026-01-01T00:00:00Z','Raw abstract',
                     '[]','2026-07-19T00:00:00Z','2026-07-19T00:00:00Z',0)
            """
        ).lastrowid
        conn.execute(
            "INSERT INTO item_sources(source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at) VALUES('a','pending',?,'https://papers.example/pending','pending','now','now')",
            (pending_id,),
        )
        conn.execute(
            "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,'a','new','2026-07-19T12:01:00Z')",
            (run_id, pending_id),
        )
        conn.commit()
        radar.briefs.ensure_fallback_briefs(conn)
        conn.execute(
            "UPDATE item_briefs SET status='professional',model='broken-test-model',"
            "brief_json='{}',validation_hash='invalid' WHERE item_id=?",
            (pending_id,),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(radar, "SITE_DIR", Path(directory)), mock.patch.dict(
            os.environ,
            {
                "RADAR_PUBLIC_BASE_URL": "https://reader.example/ssd-radar",
                "RADAR_WEBSUB_HUB": "https://hub.example/publish",
            },
        ):
            radar.build_site(conn)
            site = Path(directory)
            self.assertEqual((site / "live.xml").read_bytes(), (site / "feed.xml").read_bytes())
            live_root = ET.parse(site / "live.xml").getroot()
            self.assertEqual(live_root.findtext("channel/ttl"), "15")
            live_items = [node for node in live_root.iter() if radar.xml_local_name(node.tag) == "item"]
            self.assertEqual(
                [node.findtext("title") for node in live_items],
                ["中文：Future live item"],
            )
            live_link = live_items[0].findtext("link") or ""
            self.assertTrue(
                live_link.startswith("https://reader.example/ssd-radar/item.html?id=")
            )
            live_public_id = parse_qs(urlsplit(live_link).query)["id"][0]
            self.assertTrue(
                (site / "items" / live_public_id[:2] / f"{live_public_id}.json").is_file()
            )
            atom = {"atom": "http://www.w3.org/2005/Atom"}
            self_link = live_root.find("channel/atom:link[@rel='self']", atom)
            hub_link = live_root.find("channel/atom:link[@rel='hub']", atom)
            self.assertEqual(self_link.attrib["href"], "https://reader.example/ssd-radar/live.xml")
            self.assertEqual(hub_link.attrib["href"], "https://hub.example/publish")
            archive_files = sorted(site.glob("professional-archive-*.xml"))
            self.assertEqual(
                len(archive_files), radar.ARCHIVE_BUCKET_COUNT
            )
            self.assertEqual(archive_files[0].name, "professional-archive-01.xml")
            self.assertEqual(archive_files[-1].name, "professional-archive-32.xml")
            archive_counts = [
                sum(1 for node in ET.parse(path).getroot().iter() if radar.xml_local_name(node.tag) == "item")
                for path in archive_files
            ]
            self.assertEqual(sum(archive_counts), 1)
            self.assertEqual(max(archive_counts), 1)
            archived_titles = [
                node.findtext("title")
                for path in archive_files
                for node in ET.parse(path).getroot().findall("channel/item")
            ]
            self.assertEqual(archived_titles, ["中文：Future live item"])
            self.assertNotIn("Pending raw item", archived_titles)
            archived_links = [
                node.findtext("link") or ""
                for path in archive_files
                for node in ET.parse(path).getroot().findall("channel/item")
            ]
            self.assertEqual(len(archived_links), 1)
            self.assertTrue(
                archived_links[0].startswith(
                    "https://reader.example/ssd-radar/item.html?id="
                )
            )
            opml_bytes = (site / "netnewswire.opml").read_bytes()
            self.assertTrue(opml_bytes.startswith(b"\xef\xbb\xbf<?xml"))
            opml = ET.parse(site / "netnewswire.opml").getroot()
            urls = [node.attrib["xmlUrl"] for node in opml.iter("outline") if "xmlUrl" in node.attrib]
            self.assertEqual(urls[0], "https://reader.example/ssd-radar/live.xml")
            self.assertIn(
                "https://reader.example/ssd-radar/professional-archive-01.xml",
                urls,
            )
            self.assertIn(
                "https://reader.example/ssd-radar/professional-archive-32.xml",
                urls,
            )
            self.assertEqual(len(urls), radar.ARCHIVE_BUCKET_COUNT + 1)
            import_page = (site / "import.html").read_text(encoding="utf-8")
            self.assertIn('<meta charset="utf-8">', import_page)
            self.assertIn('download="SSD-Research-Radar.opml"', import_page)
            self.assertIn("https://reader.example/ssd-radar/netnewswire.opml", import_page)

            pending_catalogue = json.loads(
                (site / "archive.json").read_text(encoding="utf-8")
            )
            self.assertEqual(pending_catalogue["item_count"], 1)
            self.assertEqual(pending_catalogue["sources"][0]["item_count"], 1)
            self.assertTrue(
                all(
                    item["brief_status"] == "professional"
                    for item in pending_catalogue["items"]
                )
            )
            self.assertNotIn(
                "Pending raw item",
                [item["title"] for item in pending_catalogue["items"]],
            )
            self.assertNotEqual(
                conn.execute(
                    "SELECT status FROM item_briefs WHERE item_id=?", (pending_id,)
                ).fetchone()[0],
                "professional",
            )
            pending_public_id = radar.item_page.public_item_id("item:pending")
            self.assertFalse(
                (
                    site
                    / "items"
                    / pending_public_id[:2]
                    / f"{pending_public_id}.json"
                ).exists()
            )

            baseline_id = conn.execute(
                "SELECT id FROM items WHERE title='Baseline item 000'"
            ).fetchone()[0]
            mark_professional(conn, baseline_id)
            radar.build_site(conn)
            backfilled_catalogue = json.loads(
                (site / "archive.json").read_text(encoding="utf-8")
            )
            self.assertEqual(backfilled_catalogue["item_count"], 2)
            self.assertEqual(backfilled_catalogue["sources"][0]["item_count"], 2)
            self.assertTrue(
                all(
                    item["brief_status"] == "professional"
                    for item in backfilled_catalogue["items"]
                )
            )
            self.assertEqual(
                [path.name for path in sorted(site.glob("professional-archive-*.xml"))],
                [
                    f"professional-archive-{number:02d}.xml"
                    for number in range(1, radar.ARCHIVE_BUCKET_COUNT + 1)
                ],
            )
            backfilled_titles = [
                node.findtext("title")
                for path in sorted(site.glob("professional-archive-*.xml"))
                for node in ET.parse(path).getroot().findall("channel/item")
            ]
            self.assertCountEqual(
                backfilled_titles,
                ["中文：Baseline item 000", "中文：Future live item"],
            )
        conn.close()

    def test_websub_failure_keeps_event_pending_then_success_acknowledges(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(radar.SCHEMA)
        conn.execute(
            "INSERT INTO sources(id,name,kind,category,homepage,endpoint,config_json,initialized) VALUES(?,?,?,?,?,?,?,1)",
            ("a", "A", "rss", "paper", "https://example.com", "https://example.com/feed", "{}"),
        )
        item_id = conn.execute(
            """
            INSERT INTO items(canonical_key,item_type,title,normalized_title,summary,topics_json,discovered_at,updated_at,baseline)
            VALUES('x','paper','X','x','NAND evidence sentence.','[]','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0)
            """
        ).lastrowid
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','ok')").lastrowid
        conn.execute(
            "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,?,?,?)",
            (run_id, item_id, "a", "new", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        radar.briefs.ensure_fallback_briefs(conn)
        mark_professional(conn, item_id)
        environment = {
            "RADAR_PUBLIC_BASE_URL": "https://reader.example/radar/",
            "RADAR_WEBSUB_HUB": "https://hub.example/",
        }
        with mock.patch.dict(os.environ, environment), mock.patch.object(
            radar, "publish_websub", side_effect=RuntimeError("offline")
        ):
            warnings = radar.publish_pending_websub(conn)
        self.assertIn("已保留待重试", warnings[0])
        self.assertIsNone(conn.execute("SELECT websub_published_at FROM run_events").fetchone()[0])
        with mock.patch.dict(os.environ, environment), mock.patch.object(radar, "publish_websub") as publish:
            self.assertEqual(radar.publish_pending_websub(conn), [])
            publish.assert_called_once_with(
                "https://hub.example/", "https://reader.example/radar/live.xml"
            )
        self.assertIsNotNone(conn.execute("SELECT websub_published_at FROM run_events").fetchone()[0])
        conn.close()

    def test_publish_websub_posts_standard_form(self):
        response = mock.MagicMock()
        response.status = 204
        response.read.return_value = b""
        response.__enter__.return_value = response
        with mock.patch.object(radar.urllib.request, "urlopen", return_value=response) as urlopen:
            radar.publish_websub("https://hub.example/publish", "https://reader.example/live.xml")
        request = urlopen.call_args.args[0]
        form = parse_qs(request.data.decode("ascii"))
        self.assertEqual(form["hub.mode"], ["publish"])
        self.assertEqual(form["hub.url"], ["https://reader.example/live.xml"])


if __name__ == "__main__":
    unittest.main()
