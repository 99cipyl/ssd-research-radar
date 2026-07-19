import json
import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import radar


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
        second_run = conn.execute("INSERT INTO runs(started_at,status) VALUES('two','running')").lastrowid
        payload = radar.report_payload(conn, second_run, [])
        self.assertEqual(payload["new_count"], 1)
        conn.execute("UPDATE run_events SET delivered_at='now' WHERE delivered_at IS NULL")
        self.assertEqual(radar.report_payload(conn, second_run, [])["new_count"], 0)

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
        rows = conn.execute(
            "SELECT i.*,e.run_id,e.event_type,e.created_at AS event_created_at FROM run_events e JOIN items i ON i.id=e.item_id WHERE e.event_type='updated'"
        ).fetchall()
        feed = radar.rss_xml(rows)
        self.assertIn("[更新] Versioned NAND Paper", feed)
        self.assertIn(":updated", feed)

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
            self.assertEqual([node.findtext("title") for node in live_items], ["Future live item"])
            atom = {"atom": "http://www.w3.org/2005/Atom"}
            self_link = live_root.find("channel/atom:link[@rel='self']", atom)
            hub_link = live_root.find("channel/atom:link[@rel='hub']", atom)
            self.assertEqual(self_link.attrib["href"], "https://reader.example/ssd-radar/live.xml")
            self.assertEqual(hub_link.attrib["href"], "https://hub.example/publish")
            archive_files = sorted(site.glob("archive-*.xml"))
            self.assertEqual([path.name for path in archive_files], ["archive-2026-2.xml", "archive-2026.xml"])
            archive_counts = [
                sum(1 for node in ET.parse(path).getroot().iter() if radar.xml_local_name(node.tag) == "item")
                for path in archive_files
            ]
            self.assertEqual(sorted(archive_counts), [2, 350])
            opml = ET.parse(site / "netnewswire.opml").getroot()
            urls = [node.attrib["xmlUrl"] for node in opml.iter("outline") if "xmlUrl" in node.attrib]
            self.assertEqual(urls[0], "https://reader.example/ssd-radar/live.xml")
            self.assertIn("https://reader.example/ssd-radar/archive-2026.xml", urls)
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
            INSERT INTO items(canonical_key,item_type,title,normalized_title,topics_json,discovered_at,updated_at,baseline)
            VALUES('x','paper','X','x','[]','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0)
            """
        ).lastrowid
        run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES('now','ok')").lastrowid
        conn.execute(
            "INSERT INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,?,?,?)",
            (run_id, item_id, "a", "new", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
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
