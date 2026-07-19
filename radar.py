#!/usr/bin/env python3
"""SSD Research Radar: persistent history, incremental discovery, dashboard and RSS.

The program intentionally uses only Python's standard library.  It never scrapes
Google Scholar or IEEE Xplore.  OpenAlex provides the pull-based academic index;
the official Scholar/Xplore email alerts remain a separate backstop.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.json"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "radar.sqlite3"
LOCK_PATH = DATA_DIR / "sync.lock"
SITE_DIR = ROOT / "site"
REPORTS_DIR = ROOT / "reports"
USER_AGENT = "SSD-Research-Radar/1.0 (+local research archive)"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_FEED_ITEMS = 350
LOCAL_BASE_URL = "http://127.0.0.1:8765/"
UTC = dt.timezone.utc


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    homepage TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    config_json TEXT NOT NULL,
    initialized INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    last_full_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS item_sources (
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id),
    source_url TEXT,
    raw_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS item_versions (
    item_id INTEGER NOT NULL REFERENCES items(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    raw_hash TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    published_at TEXT,
    summary TEXT,
    PRIMARY KEY (item_id, source_id, raw_hash)
);

CREATE INDEX IF NOT EXISTS idx_item_versions_item ON item_versions(item_id);

CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_sources_item ON item_sources(item_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    successful_sources INTEGER NOT NULL DEFAULT 0,
    failed_sources INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    websub_published_at TEXT,
    PRIMARY KEY (run_id, item_id, event_type)
);
"""


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        if parsed is not None:
            return iso(parsed)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return iso(parsed)
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00Z"
    return None


def strip_html(value: Any) -> str:
    if value is None:
        return ""
    parser = TextExtractor()
    try:
        parser.feed(html.unescape(str(value)))
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html.unescape(str(value)))
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value.strip())
        kept = []
        for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
            if key.casefold().startswith("utm_") or key.casefold() in {"ref", "source", "campaign"}:
                continue
            kept.append((key, val))
        path = parts.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit(
            (parts.scheme.casefold(), parts.netloc.casefold(), path, urllib.parse.urlencode(kept), "")
        )
    except Exception:
        return value.strip()


def normalize_doi(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    doi = value.strip().casefold()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi or None


def reconstruct_abstract(inverted: Any) -> str:
    if not isinstance(inverted, dict):
        return ""
    positions: List[Tuple[int, str]] = []
    for word, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions.append((index, str(word)))
    positions.sort()
    return " ".join(word for _, word in positions)


TOPIC_PATTERNS: Sequence[Tuple[str, Sequence[str]]] = (
    ("FTL / GC", ("flash translation layer", " ftl ", "garbage collection", "wear leveling", "wear levelling", "block management", "mapping table")),
    ("NAND 可靠性", ("nand", "retention", "read disturb", "program disturb", "read retry", "read-retry", "scrubbing", "read reclaim", "bad block", "endurance", "ldpc", "error correction")),
    ("NVMe / ZNS / FDP", ("nvme", "zns", "zoned namespace", "flexible data placement", "fdp ssd", "nvme fdp")),
    ("KV / 计算存储", ("key-value ssd", "key value ssd", "kv ssd", "computational storage", "in-storage computing", "in storage computing")),
    ("数据中心 / QoS", ("data center", "datacenter", "cloud ssd", "qos", "quality of service", "telemetry", "ocp storage")),
)


def classify_topics(title: str, summary: str, fallback: Optional[str] = None) -> List[str]:
    haystack = f" {title} {summary} ".casefold()
    topics = [topic for topic, needles in TOPIC_PATTERNS if any(needle in haystack for needle in needles)]
    if not topics and fallback:
        topics.append(fallback)
    return topics


def content_hash(record: Dict[str, Any]) -> str:
    selected = {
        key: record.get(key)
        for key in ("title", "url", "doi", "authors", "venue", "published_at", "summary")
    }
    payload = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_key(record: Dict[str, Any], source_id: str) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"doi:{doi}"
    normalized = normalize_title(record.get("title") or "")
    if record.get("item_type") == "paper" and normalized:
        year = (record.get("published_at") or "")[:4]
        return f"paper:{normalized}:{year}"
    return f"source:{source_id}:{record['external_id']}"


def notification_relevant(source_id: str, title: str, summary: str, item_type: str) -> bool:
    text = f" {title} {summary} ".casefold()
    if source_id in {"fast_dblp", "openalex_ssd", "nvm_express_resources", "nvm_express_specs", "nvm_express_spec_archives"}:
        return True
    if source_id == "safari_eth":
        return any(term in text for term in (
            "nand", "flash", "ssd", "storage", "memory", "retention", "disturb",
            "error correction", "reliability", "nvme", "zns", "fdp", "high-bandwidth flash",
        ))
    if source_id == "nvm_express":
        return any(term in text for term in (
            "specification", "revision", "technical proposal", " ecn ", "feature", "zns", "fdp",
            "key-value", "computational storage", "virtualization", "telemetry", "migration",
            "security", "power management", "firmware", "reliability", "nvme 2.",
        ))
    if source_id == "ocp_storage":
        return any(term in text for term in (
            "specification", " ssd ", "nvme", "telemetry", "reliability", " fdp ", " zns ",
            "self-test", "self test", "sanitization", "media check", "firmware", "qualification",
            "testing", "quality of service", " qos ",
        ))
    return item_type in {"paper", "standard"}


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Existing installations created before periodic full rescans need a tiny
    # forward-only migration.  Treat the last successful baseline as a full
    # scan so upgrading does not immediately repeat a costly OpenAlex import.
    source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
    if "last_full_at" not in source_columns:
        conn.execute("ALTER TABLE sources ADD COLUMN last_full_at TEXT")
        conn.execute(
            "UPDATE sources SET last_full_at=last_success_at WHERE initialized=1 AND last_success_at IS NOT NULL"
        )
        conn.commit()
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(run_events)")}
    if "delivered_at" not in event_columns:
        conn.execute("ALTER TABLE run_events ADD COLUMN delivered_at TEXT")
        # Reports produced by an older release were already delivered.  Only
        # events created after this migration participate in the outbox.
        conn.execute("UPDATE run_events SET delivered_at=?", (iso(utcnow()),))
        conn.commit()
    if "websub_published_at" not in event_columns:
        conn.execute("ALTER TABLE run_events ADD COLUMN websub_published_at TEXT")
        conn.commit()
    # Seed one recoverable snapshot for databases created by earlier versions.
    # Future material changes append distinct hashes without overwriting these.
    conn.execute(
        """
        INSERT OR IGNORE INTO item_versions(
            item_id,source_id,raw_hash,captured_at,title,url,published_at,summary
        )
        SELECT x.item_id,x.source_id,x.raw_hash,x.first_seen_at,
               i.title,COALESCE(x.source_url,i.url),i.published_at,i.summary
        FROM item_sources x JOIN items i ON i.id=x.item_id
        """
    )
    conn.commit()
    return conn


def register_sources(conn: sqlite3.Connection, config: Dict[str, Any]) -> None:
    for source in config["sources"]:
        if not source.get("enabled", True):
            continue
        conn.execute(
            """
            INSERT INTO sources(id, name, kind, category, homepage, endpoint, config_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                kind=excluded.kind,
                category=excluded.category,
                homepage=excluded.homepage,
                endpoint=excluded.endpoint,
                config_json=excluded.config_json
            """,
            (
                source["id"], source["name"], source["kind"], source["category"],
                source["homepage"], source["endpoint"], json.dumps(source, ensure_ascii=False),
            ),
        )
    conn.commit()


def request_bytes(url: str, headers: Optional[Dict[str, str]] = None, retries: int = 3) -> Tuple[bytes, Any]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    last_error: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError(f"response too large: {length} bytes")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeded size limit")
                return data, response.headers
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                # A long Retry-After from OpenAlex means the daily budget is
                # exhausted.  Sleeping for hours inside a desktop automation
                # is harmful; record the source failure and retry next run.
                if exc.code == 429 and retry_after and retry_after.isdigit() and int(retry_after) > 60:
                    hours = max(1, round(int(retry_after) / 3600))
                    raise RuntimeError(f"daily API budget exhausted; retry in about {hours}h") from exc
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(min(delay, 20))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("request failed")


def request_json(url: str, retries: int = 3, headers: Optional[Dict[str, str]] = None) -> Tuple[Any, Any]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    data, response_headers = request_bytes(url, request_headers, retries=retries)
    return json.loads(data.decode("utf-8")), response_headers


def author_names(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, list):
        value = [value]
    names: List[str] = []
    for author in value:
        if isinstance(author, dict):
            name = author.get("text") or author.get("#text") or author.get("name")
        else:
            name = str(author)
        if name:
            names.append(strip_html(name))
    return ", ".join(names)


def fetch_dblp(source: Dict[str, Any], _full: bool, _since: Optional[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    offset = 0
    while True:
        # Smaller pages are more reliable than a single large DBLP response on
        # slow or filtered corporate networks.
        params = {"q": source["query"], "h": 100, "f": offset, "format": "json"}
        payload, _ = request_json(f"{source['endpoint']}?{urllib.parse.urlencode(params)}")
        hits_block = payload.get("result", {}).get("hits", {})
        hits = hits_block.get("hit", []) or []
        if isinstance(hits, dict):
            hits = [hits]
        for hit in hits:
            info = hit.get("info", {})
            if info.get("type") != "Conference and Workshop Papers" or info.get("venue") != "FAST":
                continue
            title = strip_html(info.get("title"))
            if not title:
                continue
            authors = info.get("authors", {}).get("author", []) if isinstance(info.get("authors"), dict) else []
            electronic = info.get("ee")
            if isinstance(electronic, list):
                electronic = electronic[0] if electronic else None
            record = {
                "external_id": info.get("key") or info.get("url") or title,
                "item_type": "paper",
                "title": title,
                "url": electronic or info.get("url"),
                "doi": info.get("doi"),
                "authors": author_names(authors),
                "venue": strip_html(info.get("venue") or "USENIX FAST"),
                "published_at": parse_datetime(str(info.get("year") or "")),
                "summary": "",
                "raw": info,
            }
            records.append(record)
        total = int(hits_block.get("@total", len(records)))
        offset += len(hits)
        if not hits or offset >= total:
            break
        time.sleep(1.0)
    return records


def fetch_page(source: Dict[str, Any], _full: bool, _since: Optional[str]) -> List[Dict[str, Any]]:
    data, _ = request_bytes(source["endpoint"], {"Accept": "text/html,application/xhtml+xml"})
    text = data.decode("utf-8", "replace")
    match = re.search(r"<main\b[^>]*>(.*?)</main>", text, flags=re.I | re.S)
    body = match.group(1) if match else text
    body = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", body, flags=re.I | re.S)
    summary = strip_html(body)[:30000]
    return [{
        "external_id": source["endpoint"],
        "item_type": "standard",
        "title": source.get("title") or "Page update",
        "url": source["endpoint"],
        "doi": None,
        "authors": "NVM Express",
        "venue": source["name"],
        "published_at": None,
        "summary": summary,
        "raw": {"sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()},
    }]


def fetch_wordpress_page(source: Dict[str, Any], _full: bool, _since: Optional[str]) -> List[Dict[str, Any]]:
    page, _ = request_json(source["endpoint"])
    title = strip_html((page.get("title") or {}).get("rendered")) or source.get("title") or "Page update"
    body = strip_html((page.get("content") or {}).get("rendered"))
    return [{
        "external_id": str(page.get("id") or source["endpoint"]),
        "item_type": "standard",
        "title": title,
        "url": page.get("link") or source["homepage"],
        "doi": None,
        "authors": "NVM Express",
        "venue": source["name"],
        "published_at": parse_datetime(page.get("modified_gmt") or page.get("date_gmt")),
        "summary": body[:30000],
        "raw": {"id": page.get("id"), "modified_gmt": page.get("modified_gmt"), "content": body},
    }]


def wordpress_url(source: Dict[str, Any], page: int, full: bool, since: Optional[str]) -> str:
    params: Dict[str, Any] = {
        "per_page": 100,
        "page": page,
        "_fields": "id,date_gmt,modified_gmt,link,title,excerpt",
    }
    if not full and since:
        params.update({"orderby": "modified", "order": "desc", "modified_after": since})
    return f"{source['endpoint']}?{urllib.parse.urlencode(params)}"


def fetch_wordpress(source: Dict[str, Any], full: bool, since: Optional[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        try:
            payload, headers = request_json(wordpress_url(source, page, full, since))
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and not full and since:
                return fetch_wordpress(source, False, None)
            raise
        total_pages = int(headers.get("X-WP-TotalPages", "1"))
        for post in payload:
            title = strip_html((post.get("title") or {}).get("rendered"))
            if not title:
                continue
            excerpt = strip_html((post.get("excerpt") or {}).get("rendered"))
            record = {
                "external_id": str(post.get("id") or post.get("link") or title),
                "item_type": "post",
                "title": title,
                "url": post.get("link"),
                "doi": None,
                "authors": "",
                "venue": source["name"],
                "published_at": parse_datetime(post.get("date_gmt")),
                "summary": excerpt,
                "raw": post,
            }
            records.append(record)
        page += 1
    return records


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(node: ET.Element, names: Sequence[str]) -> str:
    wanted = set(names)
    for child in node.iter():
        if child is node:
            continue
        if xml_local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def parse_feed(data: bytes, source: Dict[str, Any]) -> List[Dict[str, Any]]:
    upper = data[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("unsafe XML declaration")
    root = ET.fromstring(data)
    root_name = xml_local_name(root.tag).casefold()
    records: List[Dict[str, Any]] = []
    if root_name == "rss" or any(xml_local_name(node.tag) == "item" for node in root.iter()):
        nodes = [node for node in root.iter() if xml_local_name(node.tag) == "item"]
        for node in nodes:
            title = strip_html(child_text(node, ("title",)))
            link = child_text(node, ("link",))
            guid = child_text(node, ("guid", "id")) or link or title
            summary = strip_html(child_text(node, ("description", "encoded", "summary", "content")))
            records.append({
                "external_id": guid,
                "item_type": "message" if source["id"] == "ocp_storage" else "post",
                "title": title or "(untitled)",
                "url": link,
                "doi": None,
                "authors": strip_html(child_text(node, ("creator", "author"))),
                "venue": source["name"],
                "published_at": parse_datetime(child_text(node, ("pubDate", "published", "updated", "date"))),
                "summary": summary,
                "raw": {"guid": guid, "link": link},
            })
    else:
        nodes = [node for node in root.iter() if xml_local_name(node.tag) == "entry"]
        for node in nodes:
            title = strip_html(child_text(node, ("title",)))
            links = [child.attrib.get("href") for child in node if xml_local_name(child.tag) == "link" and child.attrib.get("href")]
            link = links[0] if links else ""
            external_id = child_text(node, ("id",)) or link or title
            records.append({
                "external_id": external_id,
                "item_type": "post",
                "title": title or "(untitled)",
                "url": link,
                "doi": None,
                "authors": strip_html(child_text(node, ("name", "author"))),
                "venue": source["name"],
                "published_at": parse_datetime(child_text(node, ("published", "updated"))),
                "summary": strip_html(child_text(node, ("summary", "content"))),
                "raw": {"id": external_id, "link": link},
            })
    return records


def fetch_rss(source: Dict[str, Any], _full: bool, _since: Optional[str]) -> List[Dict[str, Any]]:
    data, _ = request_bytes(source["endpoint"], {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    return parse_feed(data, source)


def groupsio_api_key() -> Optional[str]:
    key = os.environ.get("GROUPS_IO_API_KEY")
    key_file = DATA_DIR / "groupsio_api_key"
    if not key and key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
    return key or None


def fetch_groupsio(source: Dict[str, Any], full: bool, since: Optional[str]) -> List[Dict[str, Any]]:
    key = groupsio_api_key()
    if not key:
        return fetch_rss(source, full, since)
    records: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "group_id": source["group_id"], "limit": 100, "sort_dir": "asc", "extended": "true"
        }
        if page_token:
            params["page_token"] = page_token
        url = f"{source['api_endpoint']}?{urllib.parse.urlencode(params)}"
        payload, _ = request_json(url, retries=5, headers={"Authorization": f"Bearer {key}"})
        if payload.get("object") == "error":
            raise RuntimeError(f"Groups.io API: {payload.get('type')}: {payload.get('extra', '')}")
        messages = payload.get("data") or []
        for message in messages:
            number = message.get("msg_num") or message.get("id")
            title = strip_html(message.get("subject_with_tags") or message.get("subject")) or "(untitled)"
            body = strip_html(message.get("body") or message.get("snippet") or message.get("remainder"))
            records.append({
                "external_id": str(number),
                "item_type": "message",
                "title": title,
                "url": f"https://ocp-all.groups.io/g/OCP-Storage/message/{number}",
                "doi": None,
                "authors": strip_html(message.get("name")),
                "venue": source["name"],
                "published_at": parse_datetime(message.get("created")),
                "summary": body,
                "raw": message,
            })
        if not payload.get("has_more") or not messages:
            break
        page_token = str(payload.get("next_page_token"))
        time.sleep(1.0)
    return records


def relevant_openalex(text: str, query: Dict[str, Any]) -> bool:
    lowered = text.casefold()
    required_all = query.get("required_all", [])
    required_any = query.get("required_any", [])
    if required_all and not all(term.casefold() in lowered for term in required_all):
        return False
    if required_any and not any(term.casefold() in lowered for term in required_any):
        return False
    return True


def openalex_record(work: Dict[str, Any]) -> Dict[str, Any]:
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    best_oa = work.get("best_oa_location") or {}
    doi = normalize_doi(work.get("doi"))
    url = primary.get("landing_page_url") or best_oa.get("landing_page_url")
    if not url and doi:
        url = f"https://doi.org/{doi}"
    url = url or work.get("id")
    return {
        "external_id": work.get("id") or doi or work.get("title"),
        "item_type": "paper",
        "title": strip_html(work.get("title")) or "(untitled)",
        "url": url,
        "doi": doi,
        "authors": ", ".join(authors),
        "venue": strip_html(source.get("display_name")),
        "published_at": parse_datetime(work.get("publication_date")),
        "summary": abstract,
        "raw": work,
    }


def fetch_openalex(source: Dict[str, Any], full: bool, since: Optional[str]) -> List[Dict[str, Any]]:
    start = since[:10] if since else "2000-01-01"
    if not full:
        overlap = int(source.get("incremental_overlap_days", 365))
        start = (utcnow() - dt.timedelta(days=overlap)).date().isoformat()
    fields = "id,doi,title,publication_date,authorships,primary_location,best_oa_location,abstract_inverted_index,type"
    unique: Dict[str, Dict[str, Any]] = {}
    for query in source.get("queries", []):
        cursor = "*"
        page_count = 0
        while cursor:
            params = {
                "search": query["search"],
                "filter": f"from_publication_date:{start}",
                # OpenAlex's current documented maximum is 100; keeping the
                # abstract-bearing responses smaller also avoids read timeouts.
                "per-page": 100,
                "cursor": cursor,
                "select": fields,
            }
            api_key = os.environ.get("OPENALEX_API_KEY")
            if api_key:
                params["api_key"] = api_key
            url = f"{source['endpoint']}?{urllib.parse.urlencode(params)}"
            payload, _ = request_json(url, retries=5)
            results = payload.get("results") or []
            for work in results:
                if work.get("type") in {"dataset", "paratext", "reference-entry", "editorial", "letter"}:
                    continue
                record = openalex_record(work)
                searchable = f"{record['title']} {record['summary']}"
                if relevant_openalex(searchable, query):
                    unique[str(record["external_id"])] = record
            cursor = (payload.get("meta") or {}).get("next_cursor")
            page_count += 1
            if not results or page_count >= 100:
                break
    return list(unique.values())


FETCHERS = {
    "dblp": fetch_dblp,
    "wordpress": fetch_wordpress,
    "rss": fetch_rss,
    "groupsio": fetch_groupsio,
    "page": fetch_page,
    "wordpress_page": fetch_wordpress_page,
    "openalex": fetch_openalex,
}


def preferred_value(old: Optional[str], new: Optional[str], prefer_longer: bool = False) -> Optional[str]:
    if not new:
        return old
    if not old:
        return new
    if prefer_longer and len(new) > len(old):
        return new
    return old


def merged_topics(existing_json: Optional[str], incoming: Sequence[str]) -> str:
    try:
        existing = json.loads(existing_json or "[]")
    except (TypeError, ValueError):
        existing = []
    return json.dumps(list(dict.fromkeys([*existing, *incoming])), ensure_ascii=False)


def store_version(
    conn: sqlite3.Connection,
    item_id: int,
    source_id: str,
    raw_hash: str,
    captured_at: str,
    record: Dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO item_versions(
            item_id,source_id,raw_hash,captured_at,title,url,published_at,summary
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            item_id, source_id, raw_hash, captured_at, record["title"], record.get("url"),
            record.get("published_at"), record.get("summary"),
        ),
    )


def ingest_record(
    conn: sqlite3.Connection,
    run_id: int,
    source: Dict[str, Any],
    record: Dict[str, Any],
    notify: bool,
) -> Tuple[bool, bool]:
    now = iso(utcnow())
    record["title"] = strip_html(record.get("title")) or "(untitled)"
    record["summary"] = strip_html(record.get("summary"))
    record["url"] = normalize_url(record.get("url"))
    record["doi"] = normalize_doi(record.get("doi"))
    record["published_at"] = parse_datetime(record.get("published_at"))
    record["authors"] = strip_html(record.get("authors"))
    record["venue"] = strip_html(record.get("venue"))
    record["topics"] = classify_topics(record["title"], record["summary"], source.get("category"))
    effective_notify = notify and notification_relevant(
        source["id"], record["title"], record["summary"], record.get("item_type") or "post"
    )
    raw_hash = content_hash(record)
    external_id = str(record["external_id"])
    existing_source = conn.execute(
        "SELECT item_id, raw_hash FROM item_sources WHERE source_id=? AND external_id=?",
        (source["id"], external_id),
    ).fetchone()
    if existing_source:
        conn.execute(
            "UPDATE item_sources SET last_seen_at=?, source_url=?, raw_hash=? WHERE source_id=? AND external_id=?",
            (now, record.get("url"), raw_hash, source["id"], external_id),
        )
        was_updated = existing_source["raw_hash"] != raw_hash
        if was_updated:
            item = conn.execute("SELECT * FROM items WHERE id=?", (existing_source["item_id"],)).fetchone()
            conn.execute(
                """
                UPDATE items SET
                    title=?, normalized_title=?, url=?, doi=?, authors=?, venue=?, published_at=?,
                    summary=?, topics_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    record["title"] if record["title"] != "(untitled)" else item["title"],
                    normalize_title(record["title"] if record["title"] != "(untitled)" else item["title"]),
                    record.get("url") or item["url"],
                    record.get("doi") or item["doi"],
                    preferred_value(item["authors"], record.get("authors"), True),
                    record.get("venue") or item["venue"],
                    record.get("published_at") or item["published_at"],
                    record.get("summary") or item["summary"],
                    merged_topics(item["topics_json"], record["topics"]), now, item["id"],
                ),
            )
            if effective_notify:
                conn.execute(
                    "INSERT OR IGNORE INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,?,?,?)",
                    (run_id, item["id"], source["id"], "updated", now),
                )
        store_version(conn, int(existing_source["item_id"]), source["id"], raw_hash, now, record)
        return False, bool(was_updated and effective_notify)

    key = canonical_key(record, source["id"])
    item = conn.execute("SELECT * FROM items WHERE canonical_key=?", (key,)).fetchone()
    is_new = item is None
    if item is None:
        cursor = conn.execute(
            """
            INSERT INTO items(
                canonical_key,item_type,title,normalized_title,url,doi,authors,venue,published_at,
                summary,topics_json,discovered_at,updated_at,baseline
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key, record.get("item_type") or "post", record["title"], normalize_title(record["title"]),
                record.get("url"), record.get("doi"), record.get("authors"), record.get("venue"),
                record.get("published_at"), record.get("summary"),
                json.dumps(record["topics"], ensure_ascii=False), now, now, 0 if effective_notify else 1,
            ),
        )
        item_id = int(cursor.lastrowid)
    else:
        item_id = int(item["id"])
        conn.execute(
            """
            UPDATE items SET
                url=?, doi=?, authors=?, venue=?, published_at=?, summary=?, topics_json=?, updated_at=?
            WHERE id=?
            """,
            (
                preferred_value(item["url"], record.get("url")),
                preferred_value(item["doi"], record.get("doi")),
                preferred_value(item["authors"], record.get("authors"), True),
                preferred_value(item["venue"], record.get("venue")),
                preferred_value(item["published_at"], record.get("published_at")),
                preferred_value(item["summary"], record.get("summary"), True),
                merged_topics(item["topics_json"], record["topics"]), now, item_id,
            ),
        )
    conn.execute(
        """
        INSERT INTO item_sources(source_id,external_id,item_id,source_url,raw_hash,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (source["id"], external_id, item_id, record.get("url"), raw_hash, now, now),
    )
    store_version(conn, item_id, source["id"], raw_hash, now, record)
    if is_new and effective_notify:
        conn.execute(
            "INSERT OR IGNORE INTO run_events(run_id,item_id,source_id,event_type,created_at) VALUES(?,?,?,?,?)",
            (run_id, item_id, source["id"], "new", now),
        )
    return bool(is_new and effective_notify), False


def overlap_since(last_success: Optional[str], days: int = 3) -> Optional[str]:
    if not last_success:
        return None
    parsed = dt.datetime.fromisoformat(last_success.replace("Z", "+00:00"))
    return iso(parsed - dt.timedelta(days=days))


def sync_source(conn: sqlite3.Connection, run_id: int, source: Dict[str, Any], force_full: bool) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source["id"],)).fetchone()
    initialized = bool(row["initialized"])
    periodic_full = False
    if source["kind"] == "openalex" and initialized:
        last_full = parse_datetime(row["last_full_at"])
        if not last_full:
            periodic_full = True
        else:
            parsed_full = dt.datetime.fromisoformat(last_full.replace("Z", "+00:00"))
            periodic_full = utcnow() - parsed_full >= dt.timedelta(days=30)
    full = force_full or not initialized or periodic_full
    minimum_interval = int(source.get("minimum_interval_hours", 0))
    if not full and minimum_interval and row["last_success_at"]:
        last_success = dt.datetime.fromisoformat(row["last_success_at"].replace("Z", "+00:00"))
        age = utcnow() - last_success
        if age < dt.timedelta(hours=minimum_interval):
            # Academic indexing does not need the twice-daily cadence used for
            # RSS/WP sources.  A fresh successful snapshot is healthy even if
            # a redundant manual retry later exhausted the anonymous budget.
            conn.execute(
                "UPDATE sources SET last_error=NULL,failure_count=0 WHERE id=?",
                (source["id"],),
            )
            conn.commit()
            return {
                "id": source["id"], "name": source["name"], "ok": True,
                "baseline": False, "fetched": 0, "new": 0, "updated": 0,
                "skipped": True,
            }
    since = source.get("history_start_date") if full else overlap_since(row["last_success_at"])
    if source["kind"] == "openalex":
        source = dict(source)
        source["incremental_overlap_days"] = int(source.get("incremental_overlap_days", 365))
    now = iso(utcnow())
    conn.execute("UPDATE sources SET last_attempt_at=? WHERE id=?", (now, source["id"]))
    conn.commit()
    fetcher = FETCHERS[source["kind"]]
    records = fetcher(source, full, since)
    new_count = 0
    updated_count = 0
    conn.execute("BEGIN")
    try:
        for record in records:
            is_new, is_updated = ingest_record(conn, run_id, source, record, notify=initialized)
            new_count += int(is_new)
            updated_count += int(is_updated)
        source_count = conn.execute(
            "SELECT COUNT(*) FROM item_sources WHERE source_id=?", (source["id"],)
        ).fetchone()[0]
        success_at = iso(utcnow())
        if full:
            conn.execute(
                """
                UPDATE sources SET initialized=1,last_success_at=?,last_full_at=?,last_error=NULL,
                    failure_count=0,item_count=? WHERE id=?
                """,
                (success_at, success_at, source_count, source["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE sources SET initialized=1,last_success_at=?,last_error=NULL,failure_count=0,item_count=?
                WHERE id=?
                """,
                (success_at, source_count, source["id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "id": source["id"], "name": source["name"], "ok": True, "baseline": not initialized,
        "fetched": len(records), "new": new_count, "updated": updated_count,
    }


def record_source_failure(conn: sqlite3.Connection, source_id: str, error: BaseException) -> None:
    conn.execute(
        """
        UPDATE sources SET last_error=?,failure_count=failure_count+1,last_attempt_at=? WHERE id=?
        """,
        (f"{type(error).__name__}: {error}"[:1000], iso(utcnow()), source_id),
    )
    conn.commit()


def item_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*,
               GROUP_CONCAT(DISTINCT s.name) AS source_names,
               GROUP_CONCAT(DISTINCT s.id) AS source_ids,
               (SELECT COUNT(*) FROM item_versions v WHERE v.item_id=i.id) AS version_count
        FROM items i
        JOIN item_sources x ON x.item_id=i.id
        JOIN sources s ON s.id=x.source_id
        GROUP BY i.id
        ORDER BY COALESCE(i.published_at,i.discovered_at) DESC, i.id DESC
        """
    ).fetchall()


def source_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY name").fetchall()


def atomically_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def dashboard_html(items: List[Dict[str, Any]], sources: List[Dict[str, Any]], generated_at: str) -> str:
    payload = json.dumps({"items": items, "sources": sources, "generated_at": generated_at}, ensure_ascii=False)
    payload = payload.replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSD Research Radar</title>
<style>
:root{{--bg:#f4f1ea;--panel:#fffdf8;--ink:#18221c;--muted:#667068;--line:#d9ddd5;--green:#0f6b4f;--amber:#b66a18;--red:#a63d40;--shadow:0 12px 34px rgba(35,49,40,.08)}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:linear-gradient(135deg,#102c23,#164f3d);color:white;padding:40px max(24px,calc((100vw - 1180px)/2));box-shadow:var(--shadow)}}
header h1{{margin:0;font-size:clamp(30px,5vw,54px);letter-spacing:-.04em}} header p{{max-width:760px;color:#d8ebe2;margin:8px 0 0}}
main{{max-width:1180px;margin:24px auto;padding:0 20px 64px}} .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:-42px}}
.stat,.controls,.status,.card{{background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}}
.stat{{padding:16px}} .stat b{{font-size:28px;display:block;color:var(--green)}} .stat span{{color:var(--muted)}}
.controls{{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:10px;padding:14px;margin:18px 0}} input,select{{width:100%;border:1px solid var(--line);border-radius:10px;background:white;padding:11px;color:var(--ink)}}
.layout{{display:grid;grid-template-columns:1fr 280px;gap:18px}} #items{{display:grid;gap:12px}} .card{{padding:18px}}
.card h2{{font-size:18px;line-height:1.3;margin:0 0 7px}} .card h2 a{{color:var(--ink);text-decoration:none}} .card h2 a:hover{{color:var(--green);text-decoration:underline}}
.meta{{color:var(--muted);font-size:13px}} .summary{{margin:10px 0 0;color:#3e4942}} .chips{{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}} .chip{{background:#e6f2ec;color:var(--green);padding:3px 8px;border-radius:999px;font-size:12px}}
.new{{background:#fff0dc;color:var(--amber)}} .status{{padding:16px;position:sticky;top:16px;height:max-content}} .status h3{{margin-top:0}} .source{{border-top:1px solid var(--line);padding:10px 0}} .source:first-of-type{{border-top:0}} .source a{{color:var(--ink);text-decoration:none}} .source a:hover{{color:var(--green);text-decoration:underline}} .ok{{color:var(--green)}} .bad{{color:var(--red)}}
.empty{{padding:40px;text-align:center;color:var(--muted)}} .pager{{display:flex;justify-content:center;gap:10px;margin:18px}} button{{border:0;border-radius:10px;padding:9px 13px;background:var(--green);color:white;cursor:pointer}} button:disabled{{opacity:.35;cursor:not-allowed}}
footer{{color:var(--muted);font-size:13px;margin-top:24px}}
@media(max-width:850px){{.stats{{grid-template-columns:repeat(2,1fr)}}.controls{{grid-template-columns:1fr 1fr}}.layout{{grid-template-columns:1fr}}.status{{position:static}}}}
@media(max-width:520px){{.controls{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<header><h1>SSD Research Radar</h1><p>一个可搜索、可积累、可增量通知的 SSD / NAND / NVMe 资料库。数据库保留来源仍公开可访问的历史，并从首次同步起永久保存后续更新。</p></header>
<main>
  <section class="stats"><div class="stat"><b id="total">0</b><span>历史资料</span></div><div class="stat"><b id="papers">0</b><span>论文</span></div><div class="stat"><b id="recent">0</b><span>近 30 天发布</span></div><div class="stat"><b id="healthy">0</b><span>来源正常</span></div></section>
  <section class="controls"><input id="query" placeholder="搜索标题、作者、摘要……"><select id="topic"><option value="">全部主题</option></select><select id="source"><option value="">全部来源</option></select><select id="year"><option value="">全部年份</option></select></section>
  <div class="layout"><section><div id="items"></div><div class="pager"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></div></section><aside id="status" class="status"></aside></div>
  <footer>生成时间：<span id="generated"></span>。绿色“新增”表示首次基线以后发现的资料；历史基线不会触发通知。</footer>
</main>
<script id="radar-data" type="application/json">{payload}</script>
<script>
const data=JSON.parse(document.getElementById('radar-data').textContent), perPage=60; let page=1;
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const topics=[...new Set(data.items.flatMap(x=>x.topics))].sort(), years=[...new Set(data.items.map(x=>(x.published_at||'').slice(0,4)).filter(Boolean))].sort().reverse();
topics.forEach(x=>$('topic').insertAdjacentHTML('beforeend',`<option>${{esc(x)}}</option>`)); data.sources.forEach(x=>$('source').insertAdjacentHTML('beforeend',`<option value="${{esc(x.id)}}">${{esc(x.name)}}</option>`)); years.forEach(x=>$('year').insertAdjacentHTML('beforeend',`<option>${{x}}</option>`));
$('total').textContent=data.items.length; $('papers').textContent=data.items.filter(x=>x.item_type==='paper').length; const cutoff=Date.now()-30*864e5; $('recent').textContent=data.items.filter(x=>Date.parse(x.published_at)>=cutoff).length; $('healthy').textContent=data.sources.filter(x=>!x.last_error).length; $('generated').textContent=data.generated_at;
$('status').innerHTML='<h3>来源状态</h3>'+data.sources.map(s=>`<div class="source"><b><a href="${{esc(s.homepage)}}" target="_blank" rel="noopener">${{esc(s.name)}}</a></b><div class="${{s.last_error?'bad':'ok'}}">${{s.last_error?'异常':'正常'}} · ${{s.item_count}} 条</div><div class="meta">最近成功：${{esc(s.last_success_at||'尚未同步')}}</div>${{s.last_error?`<div class="meta">${{esc(s.last_error)}}</div>`:''}}</div>`).join('');
function filtered(){{const q=$('query').value.trim().toLowerCase(),t=$('topic').value,s=$('source').value,y=$('year').value;return data.items.filter(x=>(!q||`${{x.title}} ${{x.authors}} ${{x.summary}}`.toLowerCase().includes(q))&&(!t||x.topics.includes(t))&&(!s||x.source_ids.includes(s))&&(!y||(x.published_at||'').startsWith(y)));}}
function render(){{const all=filtered(),pages=Math.max(1,Math.ceil(all.length/perPage));page=Math.min(page,pages);const slice=all.slice((page-1)*perPage,page*perPage);$('items').innerHTML=slice.length?slice.map(x=>`<article class="card"><h2><a href="${{esc(x.url||'#')}}" target="_blank" rel="noopener">${{esc(x.title)}}</a></h2><div class="meta">${{esc((x.published_at||'日期未知').slice(0,10))}} · ${{esc(x.source_names.join(' / '))}}${{x.venue?' · '+esc(x.venue):''}}${{x.authors?' · '+esc(x.authors):''}}</div>${{x.summary?`<p class="summary">${{esc(x.summary.slice(0,650))}}${{x.summary.length>650?'…':''}}</p>`:''}}<div class="chips">${{x.baseline?'':'<span class="chip new">新增</span>'}}${{x.version_count>1?`<span class="chip">${{x.version_count}} 个版本</span>`:''}}${{x.topics.map(t=>`<span class="chip">${{esc(t)}}</span>`).join('')}}</div></article>`).join(''):'<div class="card empty">没有匹配的资料</div>';$('page').textContent=`第 ${{page}} / ${{pages}} 页 · ${{all.length}} 条`;$('prev').disabled=page<=1;$('next').disabled=page>=pages;}}
['query','topic','source','year'].forEach(id=>$(id).addEventListener(id==='query'?'input':'change',()=>{{page=1;render()}}));$('prev').onclick=()=>{{page--;render();scrollTo(0,0)}};$('next').onclick=()=>{{page++;render();scrollTo(0,0)}};render();
</script>
</body></html>"""


def normalize_public_base_url(value: Optional[str] = None) -> str:
    """Return a safe, directory-like base URL for generated public artifacts."""
    candidate = (value if value is not None else os.environ.get("RADAR_PUBLIC_BASE_URL", "")).strip()
    if not candidate:
        return LOCAL_BASE_URL
    parts = urllib.parse.urlsplit(candidate)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ValueError("RADAR_PUBLIC_BASE_URL 必须是完整的 http(s) URL")
    path = (parts.path.rstrip("/") + "/") if parts.path else "/"
    return urllib.parse.urlunsplit((parts.scheme.casefold(), parts.netloc, path, "", ""))


def configured_public_base_url() -> Optional[str]:
    value = os.environ.get("RADAR_PUBLIC_BASE_URL", "").strip()
    return normalize_public_base_url(value) if value else None


def public_site_url(path: str = "", base_url: Optional[str] = None) -> str:
    return urllib.parse.urljoin(normalize_public_base_url(base_url), path.lstrip("/"))


def validated_http_url(value: str, variable_name: str) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"{variable_name} 必须是完整的 http(s) URL")
    return value.strip()


def rss_xml(
    rows: Sequence[sqlite3.Row],
    *,
    feed_path: str = "live.xml",
    channel_title: str = "SSD Research Radar",
    base_url: Optional[str] = None,
    hub_url: Optional[str] = None,
    archive: bool = False,
) -> str:
    atom_namespace = "http://www.w3.org/2005/Atom"
    ET.register_namespace("atom", atom_namespace)
    base = normalize_public_base_url(base_url)
    self_url = public_site_url(feed_path, base)
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = channel_title
    ET.SubElement(channel, "link").text = base
    ET.SubElement(channel, "description").text = (
        "SSD / NAND / NVMe 历史资料归档" if archive
        else "SSD / NAND / NVMe 新增与实质更新"
    )
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(utcnow())
    ET.SubElement(channel, f"{{{atom_namespace}}}link", {
        "href": self_url, "rel": "self", "type": "application/rss+xml",
    })
    if hub_url:
        ET.SubElement(channel, f"{{{atom_namespace}}}link", {
            "href": validated_http_url(hub_url, "RADAR_WEBSUB_HUB"), "rel": "hub",
        })
    for row in rows:
        event_type = row["event_type"] if "event_type" in row.keys() else "new"
        item = ET.SubElement(channel, "item")
        prefix = "" if archive else ("[更新] " if event_type == "updated" else "")
        ET.SubElement(item, "title").text = prefix + row["title"]
        ET.SubElement(item, "link").text = row["url"] or base
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        if archive:
            stable_key = row["canonical_key"] if "canonical_key" in row.keys() else str(row["id"])
            digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:32]
            guid.text = f"urn:ssd-research-radar:item:{digest}"
        else:
            run_id = row["run_id"] if "run_id" in row.keys() else 0
            guid.text = f"urn:ssd-research-radar:event:{run_id}:{row['id']}:{event_type}"
        published = (
            row["published_at"] or row["discovered_at"] if archive
            else row["event_created_at"] if "event_created_at" in row.keys() else row["discovered_at"]
        )
        try:
            parsed = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(parsed)
        except Exception:
            pass
        description = row["summary"] or ""
        topics = ", ".join(json.loads(row["topics_json"] or "[]"))
        event_label = "历史资料" if archive else ("已有资料更新" if event_type == "updated" else "新资料")
        ET.SubElement(item, "description").text = f"{event_label}\n\n{description}\n\n主题：{topics}"
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def archive_year(row: sqlite3.Row) -> str:
    value = row["published_at"] or row["discovered_at"] or ""
    match = re.match(r"^(\d{4})", value)
    return match.group(1) if match else "unknown"


def archive_feed_specs(rows: Sequence[sqlite3.Row]) -> List[Tuple[str, str, List[sqlite3.Row]]]:
    by_year: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        by_year.setdefault(archive_year(row), []).append(row)
    years = sorted(
        by_year,
        key=lambda year: (year == "unknown", -int(year) if year.isdigit() else 0),
    )
    specs: List[Tuple[str, str, List[sqlite3.Row]]] = []
    for year in years:
        # Oldest-first chunking keeps completed chunks stable as a year grows.
        ordered = sorted(
            by_year[year],
            key=lambda row: (row["published_at"] or row["discovered_at"] or "", int(row["id"])),
        )
        chunks = [ordered[index:index + MAX_FEED_ITEMS] for index in range(0, len(ordered), MAX_FEED_ITEMS)]
        for number, chunk in enumerate(chunks, start=1):
            suffix = "" if number == 1 else f"-{number}"
            filename = f"archive-{year}{suffix}.xml"
            label = f"SSD Research Radar 历史归档 · {year}"
            if len(chunks) > 1:
                label += f" · 第 {number} 卷"
            specs.append((filename, label, chunk))
    return specs


def opml_xml(
    archive_specs: Sequence[Tuple[str, str, Sequence[sqlite3.Row]]],
    *,
    base_url: Optional[str] = None,
) -> str:
    base = normalize_public_base_url(base_url)
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "SSD Research Radar · NetNewsWire"
    ET.SubElement(head, "dateCreated").text = email.utils.format_datetime(utcnow())
    body = ET.SubElement(root, "body")
    live_group = ET.SubElement(body, "outline", {"text": "SSD 即时更新（开启通知）", "title": "SSD 即时更新（开启通知）"})
    ET.SubElement(live_group, "outline", {
        "text": "SSD Research Radar · Live",
        "title": "SSD Research Radar · Live",
        "type": "rss",
        "xmlUrl": public_site_url("live.xml", base),
        "htmlUrl": base,
    })
    archive_group = ET.SubElement(body, "outline", {"text": "SSD 历史归档（建议关闭通知）", "title": "SSD 历史归档（建议关闭通知）"})
    for filename, label, _rows in archive_specs:
        ET.SubElement(archive_group, "outline", {
            "text": label,
            "title": label,
            "type": "rss",
            "xmlUrl": public_site_url(filename, base),
            "htmlUrl": base,
        })
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def live_event_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    pending = conn.execute(
        """
        SELECT i.*,e.run_id,e.event_type,e.created_at AS event_created_at
        FROM run_events e JOIN items i ON i.id=e.item_id
        WHERE e.websub_published_at IS NULL
        ORDER BY e.created_at DESC,e.run_id DESC LIMIT ?
        """,
        (MAX_FEED_ITEMS,),
    ).fetchall()
    if pending:
        return pending
    return conn.execute(
        """
        SELECT i.*,e.run_id,e.event_type,e.created_at AS event_created_at
        FROM run_events e JOIN items i ON i.id=e.item_id
        ORDER BY e.created_at DESC,e.run_id DESC LIMIT ?
        """,
        (MAX_FEED_ITEMS,),
    ).fetchall()


def write_mobile_feeds(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> None:
    base = normalize_public_base_url()
    hub = os.environ.get("RADAR_WEBSUB_HUB", "").strip() or None
    live = rss_xml(
        live_event_rows(conn), feed_path="live.xml", channel_title="SSD Research Radar · Live",
        base_url=base, hub_url=hub,
    )
    atomically_write(SITE_DIR / "live.xml", live)
    # Compatibility alias: byte-for-byte identical and canonically self-links
    # to live.xml, so existing desktop subscriptions continue to work.
    atomically_write(SITE_DIR / "feed.xml", live)
    archive_specs = archive_feed_specs(rows)
    expected = set()
    for filename, label, chunk in archive_specs:
        expected.add(filename)
        atomically_write(
            SITE_DIR / filename,
            rss_xml(chunk, feed_path=filename, channel_title=label, base_url=base, archive=True),
        )
    for stale in SITE_DIR.glob("archive-*.xml"):
        if stale.name not in expected:
            stale.unlink()
    atomically_write(SITE_DIR / "netnewswire.opml", opml_xml(archive_specs, base_url=base))


def build_site(conn: sqlite3.Connection) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    rows = item_rows(conn)
    sources = source_rows(conn)
    items: List[Dict[str, Any]] = []
    for row in rows:
        items.append({
            "id": row["id"], "item_type": row["item_type"], "title": row["title"],
            "url": row["url"], "doi": row["doi"], "authors": row["authors"], "venue": row["venue"],
            "published_at": row["published_at"], "summary": row["summary"] or "",
            "topics": json.loads(row["topics_json"] or "[]"),
            "source_names": (row["source_names"] or "").split(","),
            "source_ids": (row["source_ids"] or "").split(","), "baseline": bool(row["baseline"]),
            "version_count": row["version_count"],
        })
    source_payload = [
        {key: row[key] for key in ("id", "name", "category", "homepage", "last_success_at", "last_error", "item_count")}
        for row in sources
    ]
    generated = iso(utcnow())
    atomically_write(SITE_DIR / "index.html", dashboard_html(items, source_payload, generated))
    write_mobile_feeds(conn, rows)
    archive = {
        "generated_at": generated,
        "item_count": len(items),
        "items": items,
        "sources": source_payload,
    }
    atomically_write(SITE_DIR / "archive.json", json.dumps(archive, ensure_ascii=False, indent=2))


def publish_websub(hub_url: str, topic_url: str, retries: int = 3) -> None:
    """Notify a WebSub hub that the public live feed should be fetched."""
    hub = validated_http_url(hub_url, "RADAR_WEBSUB_HUB")
    topic = validated_http_url(topic_url, "RADAR_PUBLIC_BASE_URL")
    body = urllib.parse.urlencode({"hub.mode": "publish", "hub.url": topic}).encode("ascii")
    last_error: Optional[BaseException] = None
    for attempt in range(retries):
        request = urllib.request.Request(
            hub,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read(1024 * 1024)
                status = getattr(response, "status", 200)
                if not 200 <= status < 300:
                    raise RuntimeError(f"WebSub hub returned HTTP {status}")
                return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"WebSub publish failed: {last_error}")


def publish_pending_websub(conn: sqlite3.Connection) -> List[str]:
    """Publish one durable event batch; leave it pending if the hub fails."""
    hub = os.environ.get("RADAR_WEBSUB_HUB", "").strip()
    if not hub:
        return []
    base = configured_public_base_url()
    if not base:
        return ["已设置 RADAR_WEBSUB_HUB，但未设置 RADAR_PUBLIC_BASE_URL；已跳过 WebSub 推送"]
    rows = conn.execute(
        """
        SELECT e.run_id,e.item_id,e.event_type
        FROM run_events e
        WHERE e.websub_published_at IS NULL
        ORDER BY e.created_at DESC,e.run_id DESC LIMIT ?
        """,
        (MAX_FEED_ITEMS,),
    ).fetchall()
    if not rows:
        return []
    try:
        publish_websub(hub, public_site_url("live.xml", base))
    except Exception as exc:
        # Do not acknowledge the WebSub outbox.  The same events remain in
        # live.xml and the next sync retries the ping.
        return [f"WebSub 推送失败，{len(rows)} 条事件已保留待重试：{type(exc).__name__}: {exc}"]
    published_at = iso(utcnow())
    conn.executemany(
        """
        UPDATE run_events SET websub_published_at=?
        WHERE run_id=? AND item_id=? AND event_type=? AND websub_published_at IS NULL
        """,
        [(published_at, row["run_id"], row["item_id"], row["event_type"]) for row in rows],
    )
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE websub_published_at IS NULL"
    ).fetchone()[0]
    if remaining:
        return [f"WebSub 已发布 {len(rows)} 条；另有 {remaining} 条积压事件将在后续同步继续发布"]
    return []


def pending_event_rows(conn: sqlite3.Connection, event_type: str) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*,s.name AS event_source
        FROM run_events e JOIN items i ON i.id=e.item_id JOIN sources s ON s.id=e.source_id
        WHERE e.delivered_at IS NULL AND e.event_type=?
        ORDER BY COALESCE(i.published_at,i.discovered_at) DESC
        """,
        (event_type,),
    ).fetchall()


def report_payload(
    conn: sqlite3.Connection,
    run_id: int,
    source_results: List[Dict[str, Any]],
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    # Read from the persistent outbox, not only this run.  If a prior process
    # exited after ingesting records but before writing latest.json, those
    # events are retried here instead of being silently lost.
    new_rows = pending_event_rows(conn, "new")
    updated_rows = pending_event_rows(conn, "updated")
    failures = [result for result in source_results if not result.get("ok")]
    return {
        "run_id": run_id,
        "ok": not failures,
        "initialized": [r["name"] for r in source_results if r.get("baseline") and r.get("ok")],
        "new_count": len(new_rows),
        "updated_count": len(updated_rows),
        "new_items": [
            {
                "title": row["title"], "url": row["url"], "published_at": row["published_at"],
                "source": row["event_source"], "authors": row["authors"],
                "topics": json.loads(row["topics_json"] or "[]"), "summary": row["summary"] or "",
            }
            for row in new_rows
        ],
        "updated_items": [
            {"title": row["title"], "url": row["url"], "source": row["event_source"]}
            for row in updated_rows
        ],
        "failures": failures,
        "warnings": warnings or [],
        "archive_path": str(SITE_DIR / "index.html"),
        "database_path": str(DB_PATH),
        "checked_at": iso(utcnow()),
    }


def markdown_report(payload: Dict[str, Any], max_items: int = 30) -> str:
    lines = [
        f"NEW_COUNT={payload['new_count']} UPDATED_COUNT={payload['updated_count']} "
        f"FAILURES={len(payload['failures'])} WARNINGS={len(payload.get('warnings', []))}",
        f"ARCHIVE={payload['archive_path']}",
    ]
    if payload["initialized"]:
        lines.append("BASELINE_INITIALIZED=" + ", ".join(payload["initialized"]))
    if (
        not payload["new_count"] and not payload["updated_count"]
        and not payload["failures"] and not payload.get("warnings")
    ):
        lines.append("NO_UPDATES")
    if payload["new_items"]:
        lines.extend(["", "## 新资料"])
        for item in payload["new_items"][:max_items]:
            date = (item.get("published_at") or "日期未知")[:10]
            topics = "/".join(item.get("topics") or [])
            lines.append(f"- [{item['title']}]({item.get('url') or ''}) — {item['source']} · {date} · {topics}")
            if item.get("summary"):
                lines.append("  " + item["summary"][:360].replace("\n", " "))
        if len(payload["new_items"]) > max_items:
            lines.append(f"- 另有 {len(payload['new_items']) - max_items} 条，请在历史面板查看。")
    if payload["updated_items"]:
        lines.extend(["", "## 已有资料发生更新"])
        for item in payload["updated_items"][:max_items]:
            lines.append(f"- [{item['title']}]({item.get('url') or ''}) — {item['source']}")
    if payload["failures"]:
        lines.extend(["", "## 抓取异常"])
        for failure in payload["failures"]:
            lines.append(f"- {failure['name']}: {failure['error']}")
    if payload.get("warnings"):
        lines.extend(["", "## 非阻断警告"])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def sync_lock() -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another sync is already running") from exc
        yield


def run_sync(force_full: bool = False, report_format: str = "markdown", max_report_items: int = 30) -> Dict[str, Any]:
    config = load_config()
    history_start = config.get("history_start_date", "2000-01-01")
    overlap_days = int(config.get("incremental_overlap_days", 365))
    with sync_lock():
        conn = connect()
        register_sources(conn, config)
        started = iso(utcnow())
        # A SIGINT, host restart, or interpreter crash can leave a run marked
        # as running.  Its undelivered events remain in the outbox and will be
        # included in this run's report.
        conn.execute(
            "UPDATE runs SET finished_at=?,status='abandoned' WHERE status='running'",
            (started,),
        )
        cursor = conn.execute("INSERT INTO runs(started_at,status) VALUES(?,?)", (started, "running"))
        run_id = int(cursor.lastrowid)
        conn.commit()
        source_results: List[Dict[str, Any]] = []
        for configured in config["sources"]:
            if not configured.get("enabled", True):
                continue
            source = dict(configured)
            source["history_start_date"] = history_start
            source["incremental_overlap_days"] = overlap_days
            try:
                result = sync_source(conn, run_id, source, force_full)
            except Exception as exc:
                record_source_failure(conn, source["id"], exc)
                result = {"id": source["id"], "name": source["name"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            source_results.append(result)
        successful = sum(1 for result in source_results if result.get("ok"))
        failed = len(source_results) - successful
        new_count = conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id=? AND event_type='new'", (run_id,)).fetchone()[0]
        updated_count = conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id=? AND event_type='updated'", (run_id,)).fetchone()[0]
        conn.execute(
            """
            UPDATE runs SET finished_at=?,status=?,successful_sources=?,failed_sources=?,new_count=?,updated_count=? WHERE id=?
            """,
            (iso(utcnow()), "ok" if not failed else "partial", successful, failed, new_count, updated_count, run_id),
        )
        conn.commit()
        build_site(conn)
        websub_warnings = publish_pending_websub(conn)
        payload = report_payload(conn, run_id, source_results, websub_warnings)
        report_text = markdown_report(payload, max_report_items)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y-%m-%dT%H%M%SZ")
        atomically_write(REPORTS_DIR / f"{stamp}.md", report_text)
        atomically_write(REPORTS_DIR / "latest.json", json.dumps(payload, ensure_ascii=False, indent=2))
        # At-least-once delivery: only acknowledge the outbox after both report
        # files are durably replaced.  A crash between these two operations can
        # repeat a notification, but cannot lose one.
        conn.execute(
            "UPDATE run_events SET delivered_at=? WHERE delivered_at IS NULL",
            (iso(utcnow()),),
        )
        conn.commit()
        conn.close()
    if report_format == "json":
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(report_text, end="")
    return payload


def command_build(_args: argparse.Namespace) -> int:
    conn = connect()
    build_site(conn)
    conn.close()
    print(SITE_DIR / "index.html")
    return 0


def command_stats(_args: argparse.Namespace) -> int:
    conn = connect()
    register_sources(conn, load_config())
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    papers = conn.execute("SELECT COUNT(*) FROM items WHERE item_type='paper'").fetchone()[0]
    print(f"历史资料: {total}\n论文: {papers}\n数据库: {DB_PATH}\n面板: {SITE_DIR / 'index.html'}")
    for row in source_rows(conn):
        state = "OK" if not row["last_error"] else "ERROR"
        print(f"{state}\t{row['name']}\t{row['item_count']}\t{row['last_success_at'] or '-'}\t{row['last_error'] or ''}")
    conn.close()
    return 0


def command_doctor(_args: argparse.Namespace) -> int:
    conn = connect()
    register_sources(conn, load_config())
    problems: List[str] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"SQLite integrity: {integrity}")
    running = conn.execute("SELECT COUNT(*) FROM runs WHERE status='running'").fetchone()[0]
    if running:
        problems.append(f"存在 {running} 个未完成同步；下次 sync 会恢复其待通知事件")
    for row in source_rows(conn):
        if not row["initialized"]:
            problems.append(f"{row['name']}: 尚未完成首次基线")
        if row["last_error"]:
            problems.append(f"{row['name']}: {row['last_error']}")
    db_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    archive_path = SITE_DIR / "archive.json"
    if archive_path.exists():
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            if int(archive.get("item_count", -1)) != db_count:
                problems.append(
                    f"archive.json 与数据库不一致：{archive.get('item_count')} != {db_count}"
                )
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"archive.json: {exc}")
    else:
        problems.append("archive.json 尚未生成")
    latest_path = REPORTS_DIR / "latest.json"
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest_run = conn.execute(
                "SELECT MAX(id) FROM runs WHERE status IN ('ok','partial')"
            ).fetchone()[0]
            if latest_run is not None and latest.get("run_id") != latest_run:
                problems.append(
                    f"latest.json 不是最近一次同步报告：{latest.get('run_id')} != {latest_run}"
                )
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"latest.json: {exc}")
    else:
        problems.append("latest.json 尚未生成")
    if (SITE_DIR / "feed.xml").exists():
        try:
            ET.parse(SITE_DIR / "feed.xml")
        except ET.ParseError as exc:
            problems.append(f"feed.xml: {exc}")
    else:
        problems.append("feed.xml 尚未生成")
    live_path = SITE_DIR / "live.xml"
    if live_path.exists():
        try:
            ET.parse(live_path)
            if (SITE_DIR / "feed.xml").exists() and live_path.read_bytes() != (SITE_DIR / "feed.xml").read_bytes():
                problems.append("feed.xml 与 live.xml 兼容别名内容不一致")
        except (ET.ParseError, OSError) as exc:
            problems.append(f"live.xml: {exc}")
    else:
        problems.append("live.xml 尚未生成")
    opml_path = SITE_DIR / "netnewswire.opml"
    if opml_path.exists():
        try:
            ET.parse(opml_path)
        except ET.ParseError as exc:
            problems.append(f"netnewswire.opml: {exc}")
    else:
        problems.append("netnewswire.opml 尚未生成")
    archived_items = 0
    for archive_feed in SITE_DIR.glob("archive-*.xml"):
        try:
            root = ET.parse(archive_feed).getroot()
            count = sum(1 for node in root.iter() if xml_local_name(node.tag) == "item")
            archived_items += count
            if count > MAX_FEED_ITEMS:
                problems.append(f"{archive_feed.name}: {count} 条，超过 {MAX_FEED_ITEMS} 条上限")
        except (ET.ParseError, OSError) as exc:
            problems.append(f"{archive_feed.name}: {exc}")
    if archived_items != db_count:
        problems.append(f"历史 RSS 条目数与数据库不一致：{archived_items} != {db_count}")
    conn.close()
    if problems:
        print("\n".join(problems))
        return 1
    print("OK: 数据库与输出文件正常")
    return 0


def command_backup(_args: argparse.Namespace) -> int:
    if not DB_PATH.exists():
        print("数据库尚不存在", file=sys.stderr)
        return 1
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"radar-{utcnow().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    source_conn = sqlite3.connect(DB_PATH)
    target_conn = sqlite3.connect(destination)
    source_conn.backup(target_conn)
    target_conn.close()
    source_conn.close()
    backups = sorted(backup_dir.glob("radar-*.sqlite3"), reverse=True)
    for old in backups[14:]:
        old.unlink()
    print(destination)
    return 0


def command_serve(args: argparse.Namespace) -> int:
    if not (SITE_DIR / "index.html").exists():
        command_build(args)
    handler = lambda *values, **kwargs: SimpleHTTPRequestHandler(*values, directory=str(SITE_DIR), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(
        f"Dashboard: {url}\nLive RSS: {url}live.xml\n"
        f"NetNewsWire OPML: {url}netnewswire.opml\n按 Ctrl+C 停止。"
    )
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SSD Research Radar")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="同步所有来源并输出增量报告")
    sync.add_argument("--full", action="store_true", help="强制重新扫描完整历史")
    sync.add_argument("--format", choices=("markdown", "json"), default="markdown")
    sync.add_argument("--max-report-items", type=int, default=30)
    sub.add_parser("build", help="从数据库重新生成面板与 RSS")
    sub.add_parser("stats", help="显示资料库与来源状态")
    sub.add_parser("doctor", help="检查数据库和输出健康状态")
    sub.add_parser("backup", help="备份 SQLite 数据库并保留最近 14 份")
    serve = sub.add_parser("serve", help="启动本机历史面板与 RSS 服务")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="自动打开浏览器")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "sync":
        payload = run_sync(args.full, args.format, args.max_report_items)
        return 0 if payload["ok"] else 2
    if args.command == "build":
        return command_build(args)
    if args.command == "stats":
        return command_stats(args)
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "backup":
        return command_backup(args)
    if args.command == "serve":
        return command_serve(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
