"""Extract clean, source-backed evidence for a radar item.

This module deliberately stops at HTML and public JSON metadata.  It does not
download papers or require a language-model service.  Callers can therefore use
it in the lightweight feed-building path and apply a separate summarizer later.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


USER_AGENT = "SSD-Research-Radar/1.0 (+public research evidence extractor)"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_EVIDENCE_CHARS = 30_000
WORDPRESS_SOURCE_IDS = {
    "safari_eth",
    "nvm_express",
    "nvm_express_resources",
    "nvm_express_specs",
    "nvm_express_spec_archives",
    "nvmw_official",
}

_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
    "ol",
}
_SKIP_TAGS = {"canvas", "footer", "form", "header", "nav", "noscript", "script", "style", "svg"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_API_URL_KEYS = ("wordpress_api_url", "source_api_url", "rest_api_url", "api_url")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read dictionaries and sqlite3.Row objects without assuming either."""

    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        keys = row.keys()
        if key in keys:
            return row[key]
    except (AttributeError, KeyError, TypeError):
        pass
    return default


def _valid_http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").casefold().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        return None
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            return None
    except ValueError:
        pass
    return value


def _same_origin(first: str, second: str) -> bool:
    left = urllib.parse.urlsplit(first)
    right = urllib.parse.urlsplit(second)
    return (
        left.scheme.casefold() == right.scheme.casefold()
        and (left.hostname or "").casefold().rstrip(".")
        == (right.hostname or "").casefold().rstrip(".")
        and (left.port or (443 if left.scheme.casefold() == "https" else 80))
        == (right.port or (443 if right.scheme.casefold() == "https" else 80))
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so a trusted URL cannot bounce into a private host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _source_context(source_ids: Any) -> Tuple[List[str], List[str]]:
    """Accept the normal ID list plus optional source/API dictionaries.

    The production query currently supplies a comma-separated ID string.  The
    mapping support lets a caller pass an already-known WordPress REST URL
    without changing this function's public signature, for example::

        {"ids": ["safari_eth"], "api_url": "https://.../wp/v2/posts/42"}
    """

    ids: List[str] = []
    api_urls: List[str] = []

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            for item in value.split(","):
                item = item.strip()
                if item and item not in ids:
                    ids.append(item)
            return
        if isinstance(value, Mapping):
            identifier = value.get("id")
            if isinstance(identifier, str) and identifier and identifier not in ids:
                ids.append(identifier)
            for key in ("ids", "source_ids"):
                if key in value:
                    visit(value[key])
            for key in _API_URL_KEYS:
                candidate = _valid_http_url(value.get(key))
                if candidate and candidate not in api_urls:
                    api_urls.append(candidate)
            candidates = value.get("api_urls")
            if isinstance(candidates, (list, tuple, set)):
                for candidate_value in candidates:
                    candidate = _valid_http_url(candidate_value)
                    if candidate and candidate not in api_urls:
                        api_urls.append(candidate)
            return
        if isinstance(value, Iterable):
            for item in value:
                visit(item)

    visit(source_ids)
    return ids, api_urls


def _clean_text(value: Any, limit: int = MAX_EVIDENCE_CHARS) -> Tuple[str, Dict[str, Any]]:
    if value is None:
        return "", {"original_length": 0, "truncated": False}
    text = str(value)
    # WordPress sometimes returns a second layer of escaped HTML entities.
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    if "<" in text and ">" in text:
        parser = _PlainTextParser()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines: List[str] = []
    blank = False
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            lines.append(line)
            blank = False
        elif lines and not blank:
            lines.append("")
            blank = True
    text = "\n".join(lines).strip()
    original_length = len(text)
    truncated = original_length > limit
    if truncated:
        text = text[:limit].rstrip() + "…"
    return text, {"original_length": original_length, "truncated": truncated}


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        if self.skip_depth:
            if tag not in _VOID_TAGS:
                self.skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self.skip_depth = 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.casefold() in _BLOCK_TAGS and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


class _EvidenceHTMLParser(HTMLParser):
    """Collect targeted regions without depending on BeautifulSoup/lxml."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[Tuple[str, bool, bool, bool, bool]] = []
        self.usenix: List[str] = []
        self.article: List[str] = []
        self.main: List[str] = []
        self.meta: Dict[str, str] = {}
        self.rest_api_urls: List[str] = []

    @staticmethod
    def _attrs(attrs: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {str(key).casefold(): value or "" for key, value in attrs}

    def _record_head_metadata(self, tag: str, attrs: Dict[str, str]) -> None:
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").casefold()
            content = attrs.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = {piece.casefold() for piece in attrs.get("rel", "").split()}
            mime = attrs.get("type", "").casefold()
            href = _valid_http_url(attrs.get("href"))
            if href and "alternate" in rel and mime in {"application/json", "application/json+oembed"}:
                if "/wp-json/wp/v2/" in href and href not in self.rest_api_urls:
                    self.rest_api_urls.append(href)

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        attr_map = self._attrs(attrs)
        self._record_head_metadata(tag, attr_map)
        parent = self.stack[-1] if self.stack else ("", False, False, False, False)
        classes = set(attr_map.get("class", "").split())
        skip = parent[4] or tag in _SKIP_TAGS
        usenix = parent[1] or "field-name-field-paper-description" in classes
        article = parent[2] or tag == "article"
        main = parent[3] or tag == "main"
        if not skip and tag in _BLOCK_TAGS:
            self._append_active("\n", usenix, article, main)
        if tag not in _VOID_TAGS:
            self.stack.append((tag, usenix, article, main, skip))

    def handle_startendtag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        self._record_head_metadata(tag, self._attrs(attrs))
        if tag in _BLOCK_TAGS and self.stack and not self.stack[-1][4]:
            current = self.stack[-1]
            self._append_active("\n", current[1], current[2], current[3])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if not self.stack:
            return
        # Real-world HTML is not always balanced. Pop through the matching tag
        # so a malformed inner element cannot keep the whole document active.
        match = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if match is None:
            return
        current = self.stack[match]
        if not current[4] and tag in _BLOCK_TAGS:
            self._append_active("\n", current[1], current[2], current[3])
        del self.stack[match:]

    def handle_data(self, data: str) -> None:
        if not self.stack:
            return
        current = self.stack[-1]
        if not current[4]:
            self._append_active(data, current[1], current[2], current[3])

    def _append_active(self, value: str, usenix: bool, article: bool, main: bool) -> None:
        if usenix:
            self.usenix.append(value)
        if article:
            self.article.append(value)
        if main:
            self.main.append(value)


def _request_bytes(url: str, timeout: float) -> Tuple[bytes, Mapping[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("evidence response exceeds size limit")
        return data, response.headers


def _decode(data: bytes, headers: Mapping[str, Any]) -> str:
    charset: Optional[str] = None
    try:
        charset = headers.get_content_charset()  # type: ignore[attr-defined]
    except AttributeError:
        content_type = str(headers.get("Content-Type", ""))
        match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
        if match:
            charset = match.group(1)
    for encoding in (charset, "utf-8", "latin-1"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _wordpress_content(payload: Any) -> Tuple[str, Optional[str]]:
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, Mapping):
        return "", None
    content = payload.get("content")
    if isinstance(content, Mapping):
        rendered = content.get("rendered")
    else:
        rendered = content
    if not rendered:
        excerpt = payload.get("excerpt")
        rendered = excerpt.get("rendered") if isinstance(excerpt, Mapping) else excerpt
    return str(rendered or ""), _valid_http_url(payload.get("link"))


def _result(
    text: str,
    level: str,
    source_url: Optional[str],
    method: str,
    source_ids: Sequence[str],
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned, cleaning = _clean_text(text)
    metadata: Dict[str, Any] = {
        "extraction_method": method,
        "source_ids": list(source_ids),
        **cleaning,
    }
    if extra:
        metadata.update(extra)
    return {
        "text": cleaned,
        "evidence_level": level if cleaned else "none",
        "source_url": source_url,
        "metadata": metadata,
    }


def enrich_item_evidence(row: Any, source_ids: Any, timeout: float = 15) -> Dict[str, Any]:
    """Return the cleanest official text available for one radar item.

    Extraction order is explicit WordPress REST content, official HTML body,
    HTML metadata, then the summary already stored in ``items``.  Every network
    and parsing failure is contained; feed generation should never fail because
    an upstream article is temporarily unavailable.
    """

    ids, api_urls = _source_context(source_ids)
    for key in _API_URL_KEYS:
        candidate = _valid_http_url(_row_get(row, key))
        if candidate and candidate not in api_urls:
            api_urls.append(candidate)

    source_url = _valid_http_url(_row_get(row, "url"))
    timeout_value = max(0.1, min(float(timeout), 60.0))
    errors: List[str] = []

    # When the caller already knows the canonical REST record, it is cleaner
    # and substantially smaller than scraping the rendered WordPress theme.
    for api_url in api_urls:
        if source_url and not _same_origin(source_url, api_url):
            errors.append("wordpress_rest:cross_origin_blocked")
            continue
        try:
            data, headers = _request_bytes(api_url, timeout_value)
            payload = json.loads(_decode(data, headers))
            rendered, canonical_url = _wordpress_content(payload)
            if rendered:
                return _result(
                    rendered,
                    "official_fulltext",
                    source_url or canonical_url or api_url,
                    "wordpress_rest_content",
                    ids,
                    {"evidence_url": api_url},
                )
        except Exception as exc:  # Upstream failures must fall back gracefully.
            errors.append(f"wordpress_rest:{type(exc).__name__}")

    if source_url:
        try:
            data, headers = _request_bytes(source_url, timeout_value)
            document = _decode(data, headers)
            parser = _EvidenceHTMLParser()
            parser.feed(document)
            parser.close()

            host = urllib.parse.urlsplit(source_url).netloc.casefold()
            is_usenix_fast = host.endswith("usenix.org") and (
                "fast_dblp" in ids or "/conference/fast" in source_url.casefold()
            )
            if is_usenix_fast:
                abstract = "".join(parser.usenix)
                cleaned, _ = _clean_text(abstract)
                if cleaned:
                    return _result(
                        abstract,
                        "official_abstract",
                        source_url,
                        "usenix_paper_description",
                        ids,
                    )
                for key in ("citation_abstract", "description", "og:description", "twitter:description"):
                    if parser.meta.get(key):
                        return _result(
                            parser.meta[key],
                            "official_metadata",
                            source_url,
                            f"html_meta_{key}",
                            ids,
                        )

            # Public WordPress pages advertise their exact REST record in a
            # head <link>.  Following that link avoids guessing a post ID and
            # also handles themes (notably nvmexpress.org) that render their
            # body inside custom divs rather than an article/main element.
            for rest_url in parser.rest_api_urls:
                if not _same_origin(source_url, rest_url):
                    errors.append("wordpress_rest_discovered:cross_origin_blocked")
                    continue
                try:
                    rest_data, rest_headers = _request_bytes(rest_url, timeout_value)
                    payload = json.loads(_decode(rest_data, rest_headers))
                    rendered, canonical_url = _wordpress_content(payload)
                    if rendered:
                        return _result(
                            rendered,
                            "official_fulltext",
                            source_url or canonical_url or rest_url,
                            "wordpress_rest_discovered",
                            ids,
                            {"evidence_url": rest_url},
                        )
                except Exception as exc:
                    errors.append(f"wordpress_rest_discovered:{type(exc).__name__}")

            # article is more precise than main on most WordPress themes.
            for region, content in (("article", parser.article), ("main", parser.main)):
                value = "".join(content)
                cleaned, _ = _clean_text(value)
                if cleaned:
                    level = "official_fulltext" if set(ids) & WORDPRESS_SOURCE_IDS else "official_page"
                    return _result(value, level, source_url, f"html_{region}", ids)

            for key in ("citation_abstract", "description", "og:description", "twitter:description"):
                if parser.meta.get(key):
                    return _result(
                        parser.meta[key],
                        "official_metadata",
                        source_url,
                        f"html_meta_{key}",
                        ids,
                    )
        except Exception as exc:
            errors.append(f"html:{type(exc).__name__}")

    fallback = _row_get(row, "summary", "") or ""
    return _result(
        fallback,
        "source_summary" if fallback else "none",
        source_url,
        "stored_summary" if fallback else "no_evidence",
        ids,
        {"errors": errors} if errors else None,
    )


__all__ = ["enrich_item_evidence"]
