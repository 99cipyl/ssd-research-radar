import http.server
import threading
import unittest
import urllib.error
from unittest import mock

import evidence


class EvidenceTests(unittest.TestCase):
    def test_valid_http_url_rejects_local_and_private_targets(self):
        rejected = (
            "http://localhost/",
            "https://LOCALHOST./",
            "http://service.localhost/",
            "https://printer.local/",
            "https://user@example.org/",
            "https://user:password@example.org/",
            "http://127.0.0.1/",
            "http://127.12.34.56/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[fc00::1]/",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(evidence._valid_http_url(url))

        self.assertEqual(evidence._valid_http_url("https://example.org/paper"), "https://example.org/paper")
        self.assertEqual(evidence._valid_http_url("https://8.8.8.8/dns"), "https://8.8.8.8/dns")
        self.assertEqual(
            evidence._valid_http_url("https://[2001:4860:4860::8888]/dns"),
            "https://[2001:4860:4860::8888]/dns",
        )

    def test_request_bytes_does_not_follow_redirect_to_loopback(self):
        requests = []

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"http://127.0.0.1:{self.server.server_port}/private",
                    )
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"private response")

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                evidence._request_bytes(
                    f"http://127.0.0.1:{server.server_port}/redirect",
                    timeout=2,
                )
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(requests, ["/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_usenix_fast_prefers_official_paper_description(self):
        page = b"""<html><head><meta name="description" content="Short metadata"></head>
        <body><main><article><div class="field field-name-field-paper-description">
        <p>Existing SSD buffering has three bottlenecks.</p>
        <p>We propose <b>WSBuffer</b> and improve throughput by 3.91x.</p>
        </div><p>Biography boilerplate.</p></article></main></body></html>"""
        with mock.patch.object(evidence, "_request_bytes", return_value=(page, {})) as request:
            result = evidence.enrich_item_evidence(
                {"url": "https://www.usenix.org/conference/fast26/presentation/zhan", "summary": ""},
                ["fast_dblp"],
                timeout=3,
            )

        request.assert_called_once()
        self.assertEqual(result["evidence_level"], "official_abstract")
        self.assertEqual(result["metadata"]["extraction_method"], "usenix_paper_description")
        self.assertIn("Existing SSD buffering", result["text"])
        self.assertIn("WSBuffer", result["text"])
        self.assertNotIn("Biography", result["text"])

    def test_usenix_fast_uses_meta_description_when_description_field_is_absent(self):
        page = b"""<html><head><meta name="description"
        content="Official FAST abstract from metadata."></head><body><div>Menu only</div></body></html>"""
        with mock.patch.object(evidence, "_request_bytes", return_value=(page, {})):
            result = evidence.enrich_item_evidence(
                {"url": "https://www.usenix.org/conference/fast25/presentation/example"},
                "fast_dblp",
            )

        self.assertEqual(result["evidence_level"], "official_metadata")
        self.assertEqual(result["text"], "Official FAST abstract from metadata.")

    def test_explicit_wordpress_api_content_is_cleaned(self):
        payload = b"""{
          "link": "https://safari.ethz.ch/seminar/",
          "content": {"rendered": "<p><b>Abstract:</b> High-bandwidth flash extends GPU memory.</p><script>bad()</script>"}
        }"""
        context = {
            "ids": ["safari_eth"],
            "api_url": "https://safari.ethz.ch/wp-json/wp/v2/posts/42",
        }
        with mock.patch.object(evidence, "_request_bytes", return_value=(payload, {})) as request:
            result = evidence.enrich_item_evidence({"summary": "short excerpt"}, context)

        request.assert_called_once_with(context["api_url"], 15.0)
        self.assertEqual(result["evidence_level"], "official_fulltext")
        self.assertEqual(result["source_url"], "https://safari.ethz.ch/seminar/")
        self.assertIn("High-bandwidth flash", result["text"])
        self.assertNotIn("bad()", result["text"])

    def test_wordpress_article_url_needs_no_post_id(self):
        page = b"""<html><body><header>Site menu</header><main>
        <article><h1>NVMe update</h1><p>The feature adds host-driven placement.</p></article>
        <aside>Unrelated</aside></main><footer>Copyright</footer></body></html>"""
        with mock.patch.object(evidence, "_request_bytes", return_value=(page, {})):
            result = evidence.enrich_item_evidence(
                {"url": "https://nvmexpress.org/nvme-update/", "summary": "excerpt"},
                ["nvm_express"],
            )

        self.assertEqual(result["evidence_level"], "official_fulltext")
        self.assertEqual(result["metadata"]["extraction_method"], "html_article")
        self.assertIn("host-driven placement", result["text"])
        self.assertNotIn("Site menu", result["text"])
        self.assertNotIn("Unrelated", result["text"])

    def test_wordpress_rest_record_is_discovered_from_article_url(self):
        page = b"""<html><head><link rel="alternate" type="application/json"
        href="https://nvmexpress.org/wp-json/wp/v2/posts/20524"></head>
        <body><div class="custom-theme-body">Not inside article or main.</div></body></html>"""
        payload = b'{"content":{"rendered":"<p>Full official NVMe article body.</p>"}}'

        def fake_request(url, _timeout):
            if url == "https://nvmexpress.org/example/":
                return page, {}
            if url == "https://nvmexpress.org/wp-json/wp/v2/posts/20524":
                return payload, {}
            raise AssertionError(url)

        with mock.patch.object(evidence, "_request_bytes", side_effect=fake_request) as request:
            result = evidence.enrich_item_evidence(
                {"url": "https://nvmexpress.org/example/", "summary": "short excerpt"},
                ["nvm_express"],
            )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["evidence_level"], "official_fulltext")
        self.assertEqual(result["metadata"]["extraction_method"], "wordpress_rest_discovered")
        self.assertEqual(result["text"], "Full official NVMe article body.")

    def test_cross_origin_wordpress_rest_link_is_not_requested(self):
        page = b"""<html><head><link rel="alternate" type="application/json"
        href="https://attacker.example/wp-json/wp/v2/posts/7"></head>
        <body>No structured article body.</body></html>"""

        def fake_request(url, _timeout):
            if url != "https://nvmexpress.org/example/":
                raise AssertionError(f"unexpected cross-origin request: {url}")
            return page, {}

        with mock.patch.object(evidence, "_request_bytes", side_effect=fake_request) as request:
            result = evidence.enrich_item_evidence(
                {
                    "url": "https://nvmexpress.org/example/",
                    "summary": "Stored official excerpt.",
                },
                ["nvm_express"],
            )

        request.assert_called_once_with("https://nvmexpress.org/example/", 15.0)
        self.assertEqual(result["evidence_level"], "source_summary")
        self.assertEqual(result["text"], "Stored official excerpt.")
        self.assertIn(
            "wordpress_rest_discovered:cross_origin_blocked",
            result["metadata"]["errors"],
        )

    def test_generic_html_falls_back_from_main_to_metadata(self):
        main_page = b"<html><body><nav>menu</nav><main><h1>Change</h1><p>New telemetry log page.</p></main></body></html>"
        with mock.patch.object(evidence, "_request_bytes", return_value=(main_page, {})):
            main = evidence.enrich_item_evidence({"url": "https://example.org/change"}, ["other"])
        self.assertEqual(main["evidence_level"], "official_page")
        self.assertEqual(main["metadata"]["extraction_method"], "html_main")

        meta_page = b'<html><head><meta property="og:description" content="Structured announcement"></head></html>'
        with mock.patch.object(evidence, "_request_bytes", return_value=(meta_page, {})):
            meta = evidence.enrich_item_evidence({"url": "https://example.org/news"}, ["other"])
        self.assertEqual(meta["evidence_level"], "official_metadata")
        self.assertEqual(meta["text"], "Structured announcement")

    def test_network_failure_returns_clean_stored_summary(self):
        with mock.patch.object(evidence, "_request_bytes", side_effect=TimeoutError("offline")):
            result = evidence.enrich_item_evidence(
                {"url": "https://example.org/paper", "summary": "<p>Known <b>abstract</b>.</p>"},
                "openalex_ssd",
            )

        self.assertEqual(result["evidence_level"], "source_summary")
        self.assertEqual(result["text"], "Known abstract.")
        self.assertEqual(result["metadata"]["errors"], ["html:TimeoutError"])

    def test_invalid_url_does_not_issue_a_request(self):
        with mock.patch.object(evidence, "_request_bytes") as request:
            result = evidence.enrich_item_evidence(
                {"url": "file:///etc/passwd", "summary": "Safe fallback"},
                ["openalex_ssd"],
            )
        request.assert_not_called()
        self.assertEqual(result["text"], "Safe fallback")
        self.assertEqual(result["source_url"], None)


if __name__ == "__main__":
    unittest.main()
