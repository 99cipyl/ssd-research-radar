#!/usr/bin/env python3
"""Prepare SSD Research Radar output for public GitHub Pages hosting."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import shutil
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence


ATOM = "http://www.w3.org/2005/Atom"
HUB_URL = "https://pubsubhubbub.appspot.com/"
UTC = dt.timezone.utc
ET.register_namespace("atom", ATOM)


def normalized_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("public base URL must be an absolute HTTPS URL")
    return value.rstrip("/") + "/"


def parsed_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
    except ValueError:
        return None


def rss_date(value: Optional[str]) -> Optional[str]:
    parsed = parsed_datetime(value)
    return email.utils.format_datetime(parsed) if parsed else None


def add_discovery_links(channel: ET.Element, feed_url: str) -> None:
    for element in list(channel.findall(f"{{{ATOM}}}link")):
        if element.get("rel") in {"self", "hub"}:
            channel.remove(element)
    ET.SubElement(
        channel, f"{{{ATOM}}}link", {"rel": "self", "type": "application/rss+xml", "href": feed_url}
    )
    ET.SubElement(channel, f"{{{ATOM}}}link", {"rel": "hub", "href": HUB_URL})


def rewrite_live_feed(path: Path, base_url: str) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise ValueError(f"{path} has no RSS channel")
    link_node = channel.find("link")
    if link_node is None:
        link_node = ET.SubElement(channel, "link")
    link_node.text = base_url
    description = channel.find("description")
    if description is not None:
        description.text = "SSD / NAND / NVMe 新增与实质更新（最近 200 个事件）"
    ttl = channel.find("ttl")
    if ttl is None:
        ttl = ET.SubElement(channel, "ttl")
    ttl.text = "15"
    feed_url = urllib.parse.urljoin(base_url, "feed.xml")
    add_discovery_links(channel, feed_url)
    for item in channel.findall("item"):
        link = item.find("link")
        if link is not None and (not link.text or link.text.startswith("http://127.0.0.1")):
            link.text = base_url
    ET.indent(root, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def add_text(parent: ET.Element, name: str, value: Optional[str]) -> None:
    if value:
        ET.SubElement(parent, name).text = value


def topic_text(raw: Optional[str]) -> str:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        values = []
    return "、".join(str(value) for value in values)


def add_feed_item(
    channel: ET.Element,
    row: sqlite3.Row,
    *,
    guid: str,
    event_type: Optional[str] = None,
    event_created_at: Optional[str] = None,
) -> None:
    item = ET.SubElement(channel, "item")
    title = row["title"]
    if event_type == "updated":
        title = "[更新] " + title
    add_text(item, "title", title)
    add_text(item, "link", row["url"])
    guid_node = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid_node.text = guid
    date = event_created_at or row["published_at"] or row["discovered_at"]
    add_text(item, "pubDate", rss_date(date))
    event_label = {
        "new": "新资料",
        "updated": "已有资料发生实质更新",
    }.get(event_type, "历史资料")
    parts = [event_label]
    if row["summary"]:
        parts.extend(("", row["summary"]))
    topics = topic_text(row["topics_json"])
    if topics:
        parts.extend(("", f"主题：{topics}"))
    add_text(item, "description", "\n".join(parts))


def build_full_feed(database: Path, destination: Path, base_url: str) -> int:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        baseline_rows = connection.execute(
            """
            SELECT * FROM items WHERE baseline=1
            ORDER BY COALESCE(published_at,discovered_at) DESC,id DESC
            """
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT i.*,e.run_id,e.event_type,e.created_at AS event_created_at
            FROM run_events e JOIN items i ON i.id=e.item_id
            ORDER BY e.created_at DESC,e.run_id DESC,i.id DESC
            """
        ).fetchall()
    finally:
        connection.close()

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    add_text(channel, "title", "SSD Research Radar｜完整历史与后续更新")
    add_text(channel, "link", base_url)
    add_text(
        channel,
        "description",
        "首次订阅可读取当前完整历史；以后继续接收新增资料和实质更新。",
    )
    add_text(channel, "language", "zh-CN")
    add_text(channel, "lastBuildDate", email.utils.format_datetime(dt.datetime.now(UTC)))
    add_text(channel, "ttl", "15")
    add_discovery_links(channel, urllib.parse.urljoin(base_url, "full.xml"))

    # Material events come first.  New post-baseline items are represented by
    # their event GUID rather than a second item GUID, preventing duplicates.
    for row in event_rows:
        add_feed_item(
            channel,
            row,
            guid=f"urn:ssd-research-radar:event:{row['run_id']}:{row['id']}:{row['event_type']}",
            event_type=row["event_type"],
            event_created_at=row["event_created_at"],
        )
    for row in baseline_rows:
        stable = hashlib.sha256(row["canonical_key"].encode("utf-8")).hexdigest()
        add_feed_item(channel, row, guid=f"urn:ssd-research-radar:item:{stable}")

    ET.indent(root, space="  ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return len(baseline_rows) + len(event_rows)


def build_opml(destination: Path, base_url: str, generated_opml: Optional[Path] = None) -> None:
    # radar.py already emits the preferred NetNewsWire layout: one live feed
    # plus year/chunk archives capped at 350 items. Keep subscriptions.opml as
    # a compatibility alias instead of collapsing the history into a single
    # 2,900+ item feed that server readers may truncate.
    if generated_opml and generated_opml.is_file():
        shutil.copyfile(generated_opml, destination)
        return
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    add_text(head, "title", "SSD Research Radar")
    body = ET.SubElement(root, "body")
    ET.SubElement(
        body,
        "outline",
        {
            "text": "SSD Research Radar｜完整历史与后续更新",
            "title": "SSD Research Radar｜完整历史与后续更新",
            "type": "rss",
            "xmlUrl": urllib.parse.urljoin(base_url, "full.xml"),
            "htmlUrl": base_url,
        },
    )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


def build_status(report_path: Path, destination: Path) -> None:
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    public = {
        "ok": bool(report.get("ok")),
        "checked_at": report.get("checked_at"),
        "new_count": int(report.get("new_count", 0)),
        "updated_count": int(report.get("updated_count", 0)),
        "initialized": report.get("initialized", []),
        "failures": [
            {
                key: failure.get(key)
                for key in ("id", "name", "error")
                if failure.get(key) is not None
            }
            for failure in report.get("failures", [])
        ],
    }
    destination.write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def inject_subscription_links(index_path: Path, base_url: str) -> None:
    text = index_path.read_text(encoding="utf-8")
    alternate = (
        f'<link rel="alternate" type="application/rss+xml" '
        f'title="SSD Research Radar" href="{html.escape(urllib.parse.urljoin(base_url, "full.xml"))}">'
    )
    if "application/rss+xml" not in text:
        text = text.replace("</head>", alternate + "\n</head>", 1)
    banner = (
        '<p><a href="full.xml" style="color:#fff;font-weight:700">订阅完整历史 + 后续更新 RSS</a>'
        '　·　<a href="import.html" style="color:#d8ebe2">导入 NetNewsWire OPML</a></p>'
    )
    marker = "</header>"
    if "import.html" not in text:
        text = text.replace(marker, banner + marker, 1)
    index_path.write_text(text, encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument("--database", type=Path, default=Path("data/radar.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("reports/latest.json"))
    parser.add_argument("--base-url", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = normalized_base_url(args.base_url)
    if not args.database.is_file():
        raise FileNotFoundError(args.database)
    args.site.mkdir(parents=True, exist_ok=True)
    rewrite_live_feed(args.site / "feed.xml", base_url)
    count = build_full_feed(args.database, args.site / "full.xml", base_url)
    build_opml(
        args.site / "subscriptions.opml",
        base_url,
        generated_opml=args.site / "netnewswire.opml",
    )
    build_status(args.report, args.site / "status.json")
    inject_subscription_links(args.site / "index.html", base_url)
    (args.site / ".nojekyll").touch()
    print(f"Prepared public site with {count} full-feed entries: {base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
