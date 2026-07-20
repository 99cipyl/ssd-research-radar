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
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import briefs
import item_page
import retention


ATOM = "http://www.w3.org/2005/Atom"
HUB_URL = "https://pubsubhubbub.appspot.com/"
UTC = dt.timezone.utc
MAX_REPORT_AGE = dt.timedelta(days=2)
ET.register_namespace("atom", ATOM)


def normalized_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("public base URL must be an absolute HTTPS URL")
    return value.rstrip("/") + "/"


def parsed_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
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


def rewrite_live_feed(
    path: Path,
    base_url: str,
    *,
    history_window_years: int = retention.DEFAULT_HISTORY_WINDOW_YEARS,
    history_cutoff: str,
) -> None:
    history_cutoff = validate_history_cutoff(history_cutoff)
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
        description.text = (
            f"SSD / NAND / NVMe 当前新增与实质更新；事件按发生时间保留滚动最近 "
            f"{history_window_years} 年，旧资料今天发生的实质更新仍会发布，迟发现的"
            f"窗口外旧资料不作为新增（最近 350 个事件；截止日 {history_cutoff}）"
        )
    ttl = channel.find("ttl")
    if ttl is None:
        ttl = ET.SubElement(channel, "ttl")
    ttl.text = "15"
    feed_url = urllib.parse.urljoin(base_url, "live.xml")
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
    base_url: str,
) -> None:
    if row["brief_status"] != "professional":
        raise ValueError("feed rows must have a validated professional brief")
    brief = briefs.parse_brief(row["brief_json"])
    item = ET.SubElement(channel, "item")
    title = brief["title_zh"]
    if event_type == "updated":
        title = "[更新] " + title
    add_text(item, "title", title)
    event = None
    if event_type:
        event = f"{row['run_id']}:{row['id']}:{event_type}"
    add_text(item, "link", item_page.item_page_url(row["canonical_key"], base_url, event))
    guid_node = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid_node.text = guid
    date = event_created_at or row["published_at"] or row["discovered_at"]
    add_text(item, "pubDate", rss_date(date))
    event_label = {
        "new": "新资料",
        "updated": "已有资料发生实质更新",
    }.get(event_type, "历史资料")
    brief_label = "已整理为中文专业简报"
    parts = [event_label + " · " + brief_label, "", briefs.feed_description(brief, row["url"])]
    parts.extend(("", "点击条目先看站内简报；原文入口位于简报内。"))
    add_text(item, "description", "\n".join(parts))


def build_full_feed(
    database: Path,
    destination: Path,
    base_url: str,
    *,
    history_cutoff: str,
    history_window_years: int = retention.DEFAULT_HISTORY_WINDOW_YEARS,
) -> int:
    validate_history_cutoff(history_cutoff)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        briefs.validate_professional_briefs(connection)
        baseline_rows = connection.execute(
            f"""
            SELECT i.*,b.brief_json,b.status AS brief_status
            FROM items i JOIN item_briefs b ON b.item_id=i.id
            WHERE i.baseline=1 AND b.status='professional'
              AND {retention.item_date_sql('i')}>=?
            ORDER BY COALESCE(published_at,discovered_at) DESC,id DESC
            """,
            (history_cutoff,),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT i.*,b.brief_json,b.status AS brief_status,
                   e.run_id,e.event_type,e.created_at AS event_created_at
            FROM run_events e JOIN items i ON i.id=e.item_id
            JOIN item_briefs b ON b.item_id=i.id AND b.status='professional'
            WHERE e.suppressed_at IS NULL
              AND SUBSTR(e.created_at,1,10)>=?
            ORDER BY e.created_at DESC,e.run_id DESC,i.id DESC
            """,
            (history_cutoff,),
        ).fetchall()
        event_rows = [
            row
            for row in event_rows
            if retention.event_is_in_scope(
                row["event_type"],
                row["original_published_at"],
                row["event_created_at"],
                history_cutoff,
            )
        ]
    finally:
        connection.close()

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    add_text(channel, "title", "SSD Research Radar｜专业简报历史与后续更新")
    add_text(channel, "link", base_url)
    add_text(
        channel,
        "description",
        f"历史快照只发布首发日在滚动最近 {history_window_years} 年内、已经完成中文专业整理"
        f"并通过证据校验的资料；当前事件按发生时间收录，旧资料今天发生的实质更新仍可发布，"
        f"迟发现的窗口外旧资料不作为新增；截止日 {history_cutoff}。",
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
            base_url=base_url,
        )
    for row in baseline_rows:
        stable = hashlib.sha256(row["canonical_key"].encode("utf-8")).hexdigest()
        add_feed_item(
            channel,
            row,
            guid=f"urn:ssd-research-radar:item:{stable}",
            base_url=base_url,
        )

    ET.indent(root, space="  ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return len(baseline_rows) + len(event_rows)


def build_opml(
    destination: Path,
    base_url: str,
    generated_opml: Optional[Path] = None,
    *,
    history_window_years: int = retention.DEFAULT_HISTORY_WINDOW_YEARS,
) -> None:
    # radar.py already emits the preferred NetNewsWire layout: one live feed
    # plus pre-created stable hash buckets. Keep subscriptions.opml as a
    # compatibility alias instead of collapsing the rolling history into one
    # large feed that server readers may truncate.
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
            "text": f"SSD Research Radar｜滚动 {history_window_years} 年专业简报与后续更新",
            "title": f"SSD Research Radar｜滚动 {history_window_years} 年专业简报与后续更新",
            "type": "rss",
            "xmlUrl": urllib.parse.urljoin(base_url, "full.xml"),
            "htmlUrl": base_url,
        },
    )
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


def load_report(report_path: Path) -> Dict[str, Any]:
    if not report_path.is_file():
        raise FileNotFoundError(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("latest report must be a JSON object")
    return report


def validate_history_cutoff(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("latest report is missing history_cutoff")
    cutoff = value.strip()
    try:
        parsed = dt.date.fromisoformat(cutoff)
    except ValueError as exc:
        raise ValueError("latest report history_cutoff must be an ISO date") from exc
    if parsed.isoformat() != cutoff:
        raise ValueError("latest report history_cutoff must be an ISO date")
    return cutoff


def report_history_policy(report: Dict[str, Any]) -> tuple[str, int]:
    cutoff = validate_history_cutoff(report.get("history_cutoff"))
    try:
        window_years = retention.history_window_years(report)
    except (TypeError, ValueError) as exc:
        raise ValueError("latest report has invalid history_window_years") from exc
    checked_at = parsed_datetime(report.get("checked_at"))
    if checked_at is None:
        raise ValueError("latest report is missing a valid checked_at timestamp")
    reference_text = report.get("history_reference_date")
    if not isinstance(reference_text, str) or not reference_text.strip():
        raise ValueError("latest report is missing history_reference_date")
    try:
        reference_date = dt.date.fromisoformat(reference_text.strip())
    except ValueError as exc:
        raise ValueError(
            "latest report history_reference_date must be an ISO date"
        ) from exc
    if reference_date.isoformat() != reference_text.strip():
        raise ValueError("latest report history_reference_date must be an ISO date")
    now = dt.datetime.now(UTC)
    age = now - checked_at
    if age > MAX_REPORT_AGE or age < -dt.timedelta(minutes=10):
        raise ValueError("latest report is stale or has an invalid future timestamp")
    run_days = (checked_at.date() - reference_date).days
    if run_days not in (0, 1):
        raise ValueError(
            "latest report history_reference_date is inconsistent with checked_at"
        )
    expected_cutoff = retention.history_cutoff(
        {
            "history_window_years": window_years,
            "history_start_date": str(report.get("history_start_date") or ""),
        },
        today=reference_date,
    )
    if cutoff != expected_cutoff:
        raise ValueError(
            "latest report history_cutoff does not match its rolling history policy"
        )
    return cutoff, window_years


def build_status(
    report_path: Path,
    destination: Path,
    *,
    report: Optional[Dict[str, Any]] = None,
) -> None:
    report = report if report is not None else load_report(report_path)
    history_cutoff, history_window_years = report_history_policy(report)
    public = {
        "ok": bool(report.get("ok")),
        "source_failure_count": int(report.get("source_failure_count", 0)),
        "brief_generation_ok": bool(report.get("brief_generation_ok", True)),
        "brief_generation_failure_count": int(
            report.get("brief_generation_failure_count", 0)
        ),
        "checked_at": report.get("checked_at"),
        "history_window_years": history_window_years,
        "history_cutoff": history_cutoff,
        "history_reference_date": report.get("history_reference_date"),
        "new_count": int(report.get("new_count", 0)),
        "updated_count": int(report.get("updated_count", 0)),
        "awaiting_brief_count": int(report.get("awaiting_brief_count", 0)),
        "total_item_count": int(report.get("total_item_count", 0)),
        "professional_brief_count": int(report.get("professional_brief_count", 0)),
        "pending_history_brief_count": int(
            report.get("pending_history_brief_count", 0)
        ),
        "retry_brief_count": int(report.get("retry_brief_count", 0)),
        "backfill_percent": float(report.get("backfill_percent", 0.0)),
        "initialized": report.get("initialized", []),
        "failures": [
            {
                key: failure.get(key)
                for key in ("id", "name", "failed_count", "error")
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
        '<p><a href="full.xml" style="color:#fff;font-weight:700">订阅专业简报历史 + 后续更新 RSS</a>'
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
    report = load_report(args.report)
    history_cutoff, history_window_years = report_history_policy(report)
    args.site.mkdir(parents=True, exist_ok=True)
    rewrite_live_feed(
        args.site / "live.xml",
        base_url,
        history_window_years=history_window_years,
        history_cutoff=history_cutoff,
    )
    # Keep the long-standing feed.xml subscription as a byte-for-byte alias;
    # the canonical WebSub topic remains live.xml.
    shutil.copyfile(args.site / "live.xml", args.site / "feed.xml")
    count = build_full_feed(
        args.database,
        args.site / "full.xml",
        base_url,
        history_cutoff=history_cutoff,
        history_window_years=history_window_years,
    )
    build_opml(
        args.site / "subscriptions.opml",
        base_url,
        generated_opml=args.site / "netnewswire.opml",
        history_window_years=history_window_years,
    )
    build_status(args.report, args.site / "status.json", report=report)
    inject_subscription_links(args.site / "index.html", base_url)
    (args.site / ".nojekyll").touch()
    print(f"Prepared public site with {count} full-feed entries: {base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
