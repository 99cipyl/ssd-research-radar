#!/usr/bin/env python3
"""Export small, mobile-friendly research-brief pages for individual items.

The public page is deliberately data-independent: item data lives in one JSON
file per canonical item and is inserted into the DOM with ``textContent``.  A
reader therefore downloads only the selected item, while arbitrary titles and
abstracts can never become executable markup in ``item.html``.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import briefs as brief_engine


BRIEF_TEXT_FIELDS = (
    "title_zh",
    "one_liner",
    "what_it_is",
    "problem",
    "core_idea",
    "supporting_quote",
    "mechanism",
    "evidence",
    "engineering_relevance",
    "reading_guide",
    "limitations",
    "evidence_level",
)

MISSING_SECTION = "当前没有足够的来源信息，尚不能可靠概括这一项。"


def public_item_id(canonical_key: str) -> str:
    """Return the stable, non-sequential public id for a canonical item."""

    if not isinstance(canonical_key, str) or not canonical_key:
        raise ValueError("canonical_key must be a non-empty string")
    return hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:32]


def _normalised_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    path = parsed.path.rstrip("/") + "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def item_page_url(canonical_key: str, base_url: str, event: Optional[Any] = None) -> str:
    """Build a public detail-page URL for an item and optional run event."""

    query: Dict[str, str] = {"id": public_item_id(canonical_key)}
    if event is not None and str(event).strip():
        query["event"] = str(event).strip()
    page = urllib.parse.urljoin(_normalised_base_url(base_url), "item.html")
    return page + "?" + urllib.parse.urlencode(query)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _rows(connection: sqlite3.Connection, query: str, parameters: Sequence[Any] = ()) -> list[Dict[str, Any]]:
    cursor = connection.execute(query, parameters)
    columns = [description[0] for description in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    # All callers use module-owned constant table names, never user input.
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _safe_http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    return candidate if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _topics(raw: Any) -> list[str]:
    try:
        values = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        values = []
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _strict_fallback() -> Dict[str, Any]:
    """Return a conspicuous fallback without drawing conclusions from a title."""

    return {
        "title_zh": "",
        "one_liner": "这条资料尚未完成专业整理；当前只展示可核验的来源信息和原始摘要。",
        "what_it_is": MISSING_SECTION,
        "problem": MISSING_SECTION,
        "core_idea": MISSING_SECTION,
        "supporting_quote": "原页面未提供可逐字核验的核心思想原文依据。",
        "mechanism": MISSING_SECTION,
        "evidence": "来源未提供或尚未提取出可核验的实验结果。",
        "system_layers": [],
        "engineering_relevance": MISSING_SECTION,
        "reading_guide": "建议暂时查看原始摘要；专业简报生成后，本页会自动更新。",
        "limitations": "尚未获得足够正文信息，不能仅依据标题推断结论。",
        "evidence_level": "待整理（未依据标题推断）",
    }


def _normalise_brief_content(raw: Any) -> Dict[str, Any]:
    fallback = _strict_fallback()
    if not isinstance(raw, Mapping):
        return fallback
    result: Dict[str, Any] = {}
    for field in BRIEF_TEXT_FIELDS:
        result[field] = _string(raw.get(field)) or fallback[field]
    layers = raw.get("system_layers")
    if not isinstance(layers, list):
        layers = []
    result["system_layers"] = [
        value.strip() for value in layers if isinstance(value, str) and value.strip()
    ]
    return result


def _brief_rows(connection: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    columns = _table_columns(connection, "item_briefs")
    if not columns or "item_id" not in columns:
        return {}
    selected = [
        column
        for column in (
            "item_id",
            "status",
            "model",
            "brief_json",
            "generated_at",
            "source_hash",
            "validation_hash",
            "error",
            "last_error",
            "attempt_count",
        )
        if column in columns
    ]
    if "brief_json" not in selected:
        return {}
    return {
        int(row["item_id"]): row
        for row in _rows(connection, f"SELECT {','.join(selected)} FROM item_briefs")
    }


def _brief_payload(row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {
            "status": "missing",
            "is_professional": False,
            "model": "",
            "generated_at": "",
            "content": _strict_fallback(),
        }
    status = _string(row.get("status")) or "fallback"
    encoded = row.get("brief_json") or "{}"
    if status == "professional":
        # Professional output is an all-or-nothing contract.  Rendering a
        # partially valid model response as professional would make omitted or
        # mistyped sections indistinguishable from verified analysis.
        try:
            parsed = brief_engine.validate_professional_record(row)
        except (TypeError, ValueError):
            parsed = {}
            status = "failed"
    else:
        # Never render rejected/retry model JSON.  A demoted row may still be
        # present in an older state snapshot; only a conspicuous deterministic
        # placeholder is safe until a fresh professional brief succeeds.
        parsed = {}
    return {
        "status": status,
        "is_professional": status == "professional",
        "model": _string(row.get("model")),
        "generated_at": _string(row.get("generated_at")),
        "content": _normalise_brief_content(parsed),
    }


def _group_by(rows: Iterable[Mapping[str, Any]], key: str) -> Dict[int, list[Dict[str, Any]]]:
    grouped: Dict[int, list[Dict[str, Any]]] = {}
    for row in rows:
        item_id = int(row[key])
        grouped.setdefault(item_id, []).append(dict(row))
    return grouped


def _source_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": _string(row.get("source_id")),
        "name": _string(row.get("source_name")) or _string(row.get("source_id")),
        "category": _string(row.get("category")),
        "homepage": _safe_http_url(row.get("homepage")),
        "external_id": _string(row.get("external_id")),
        "source_url": _safe_http_url(row.get("source_url")),
        "first_seen_at": _string(row.get("first_seen_at")),
        "last_seen_at": _string(row.get("last_seen_at")),
    }


def _version_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "source_id": _string(row.get("source_id")),
        "source_name": _string(row.get("source_name")) or _string(row.get("source_id")),
        "captured_at": _string(row.get("captured_at")),
        "title": _string(row.get("title")),
        "url": _safe_http_url(row.get("url")),
        "published_at": _string(row.get("published_at")),
    }


def _event_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    run_id = row.get("run_id")
    item_id = row.get("item_id")
    event_type = _string(row.get("event_type"))
    return {
        "event_id": f"{run_id}:{item_id}:{event_type}",
        "run_id": run_id,
        "event_type": event_type,
        "created_at": _string(row.get("created_at")),
        "source_id": _string(row.get("source_id")),
        "source_name": _string(row.get("source_name")) or _string(row.get("source_id")),
        "delivered_at": _string(row.get("delivered_at")),
        "websub_published_at": _string(row.get("websub_published_at")),
    }


def _item_html(base_url: str) -> str:
    home_url = html.escape(_normalised_base_url(base_url), quote=True)
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>SSD Research Radar｜中文研究简报</title>
<style>
:root{--bg:#f4f2ec;--paper:#fffefa;--ink:#18231e;--muted:#65716a;--line:#dce2dc;--green:#145c46;--soft:#e8f1ec;--amber:#9a5b13;--shadow:0 14px 38px rgba(20,46,35,.09)}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;color:var(--ink);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--green)}.top{padding:18px max(18px,calc((100vw - 860px)/2));background:#143a2d;color:#fff}.back{color:#dcefe6;text-decoration:none;font-weight:650}.wrap{width:min(860px,100%);margin:auto;padding:22px 16px 64px}
.hero,.card{background:var(--paper);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}.hero{padding:24px;margin-bottom:16px}.eyebrow{color:var(--green);font-size:13px;font-weight:800;letter-spacing:.09em;text-transform:uppercase}.badge{display:inline-flex;margin-top:12px;padding:5px 10px;border-radius:999px;background:var(--soft);color:var(--green);font-size:13px;font-weight:750}.badge.pending{background:#fff1da;color:var(--amber)}
h1{font-size:clamp(25px,6vw,40px);line-height:1.25;margin:8px 0 10px;overflow-wrap:anywhere}.original-title{color:var(--muted);font-size:14px;overflow-wrap:anywhere}.meta{color:var(--muted);font-size:14px;margin-top:12px}.lead{margin:20px 0 0;padding:16px 18px;border-left:4px solid var(--green);background:var(--soft);border-radius:8px;font-size:18px;font-weight:650}
.grid{display:grid;grid-template-columns:1fr;gap:14px}.card{padding:20px}.card h2{font-size:18px;line-height:1.3;margin:0 0 9px}.card p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.wide{grid-column:1/-1}.chips{display:flex;flex-wrap:wrap;gap:8px}.chip{padding:5px 10px;border:1px solid #bad0c4;border-radius:999px;background:#f7fbf8;font-size:13px}
details{margin-top:16px;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:15px 18px}summary{cursor:pointer;font-weight:750}.raw{white-space:pre-wrap;overflow-wrap:anywhere;color:#34443c;margin-top:12px}.list{margin:10px 0 0;padding-left:20px}.list li+li{margin-top:10px}.action{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.button{display:inline-flex;align-items:center;min-height:46px;padding:10px 17px;border-radius:12px;background:var(--green);color:#fff;text-decoration:none;font-weight:750}.button.secondary{background:#e6ece8;color:var(--ink)}.hidden{display:none!important}.error{padding:30px;text-align:center;color:#7c2929}
@media(min-width:720px){.wrap{padding-top:34px}.hero{padding:34px}.grid{grid-template-columns:1fr 1fr}.card{padding:24px}}
</style>
</head>
<body>
<nav class="top"><a class="back" href="__HOME__">← SSD Research Radar</a></nav>
<main class="wrap">
  <section id="loading" class="hero" role="status" aria-live="polite"><h1>正在加载研究简报…</h1><p>正在读取这条资料的结构化中文整理。</p></section>
  <noscript><section class="hero error"><h1>需要启用 JavaScript</h1><p>详情页按需读取单条简报数据；请启用 JavaScript 后重新打开。</p></section></noscript>
  <article id="content" class="hidden">
    <header class="hero">
      <div class="eyebrow">中文专业研究简报</div>
      <h1 id="title">资料详情</h1>
      <div id="original-title" class="original-title hidden"></div>
      <div id="brief-status" class="badge"></div>
      <div id="meta" class="meta"></div>
      <div id="brief-provenance" class="meta"></div>
      <p id="one-liner" class="lead"></p>
      <div class="action"><a id="original-link" class="button hidden" target="_blank" rel="noopener noreferrer">阅读原文</a></div>
    </header>
    <div class="grid">
      <section class="card"><h2>内容是什么</h2><p id="what-it-is"></p></section>
      <section class="card"><h2>要解决的问题</h2><p id="problem"></p></section>
      <section class="card wide"><h2>核心思想</h2><p id="core-idea"></p></section>
      <section class="card wide"><h2>核心思想的原文依据</h2><p id="supporting-quote"></p></section>
      <section class="card"><h2>机制 / 怎么做</h2><p id="mechanism"></p></section>
      <section class="card"><h2>证据 / 结果</h2><p id="evidence"></p></section>
      <section class="card"><h2>位于 SSD 全链路哪一层</h2><div id="layers" class="chips"></div></section>
      <section class="card"><h2>与你的工程工作有什么关系</h2><p id="engineering-relevance"></p></section>
      <section class="card"><h2>怎么读最划算</h2><p id="reading-guide"></p></section>
      <section class="card"><h2>局限与阅读边界</h2><p id="limitations"></p></section>
      <section class="card wide"><h2>证据等级</h2><p id="evidence-level"></p></section>
    </div>
    <details><summary>原始摘要（用于核对，不代替专业简报）</summary><div id="raw-summary" class="raw"></div></details>
    <details><summary>来源</summary><ul id="sources" class="list"></ul></details>
    <details><summary>事件与版本</summary><div id="selected-event" class="raw"></div><ul id="events" class="list"></ul><ul id="versions" class="list"></ul></details>
  </article>
  <section id="error" class="hero error hidden" role="alert" aria-live="assertive"><h1>暂时无法打开这条简报</h1><p id="error-message"></p></section>
</main>
<script>
"use strict";
const byId = (id) => document.getElementById(id);
const text = (id, value, empty = "来源未提供") => { byId(id).textContent = value || empty; };
const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
const safeHttpUrl = (value) => {
  try { const parsed = new URL(value); return (parsed.protocol === "https:" || parsed.protocol === "http:") ? parsed.href : ""; }
  catch (_) { return ""; }
};
const dateText = (value) => {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {dateStyle:"medium", timeStyle:"short"}).format(parsed);
};
const addListText = (list, primary, secondary, href) => {
  const li = document.createElement("li");
  const url = safeHttpUrl(href);
  if (url) { const a = document.createElement("a"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = primary || url; li.appendChild(a); }
  else { const strong = document.createElement("strong"); strong.textContent = primary || "未命名来源"; li.appendChild(strong); }
  if (secondary) { const span = document.createElement("span"); span.textContent = " · " + secondary; li.appendChild(span); }
  list.appendChild(li);
};
const fail = (message) => { byId("loading").classList.add("hidden"); byId("content").classList.add("hidden"); byId("error").classList.remove("hidden"); text("error-message", message); };
const render = (data, selectedEvent) => {
  const brief = (data.brief && data.brief.content) || {};
  const displayTitle = brief.title_zh || data.title || "未命名资料";
  text("title", displayTitle);
  document.title = displayTitle + "｜SSD Research Radar";
  if (brief.title_zh && data.title && brief.title_zh !== data.title) { text("original-title", "原标题：" + data.title); byId("original-title").classList.remove("hidden"); }
  const status = byId("brief-status");
  status.textContent = data.brief && data.brief.is_professional ? "AI 自动整理完成 · 未经人工全文复核" : "待专业整理 · 不根据标题推断";
  status.classList.toggle("pending", !(data.brief && data.brief.is_professional));
  const meta = [data.published_at ? "发布 " + dateText(data.published_at) : "", data.venue || "", (data.authors || "")].filter(Boolean);
  text("meta", meta.join(" · "), "发布时间、作者或会议暂无记录");
  const briefMeta = data.brief && data.brief.is_professional
    ? [data.brief.model ? "整理模型：" + data.brief.model : "整理模型：未记录", data.brief.generated_at ? "生成时间：" + dateText(data.brief.generated_at) : "生成时间：未记录"]
    : ["专业简报尚未生成", data.brief && data.brief.generated_at ? "当前占位内容更新时间：" + dateText(data.brief.generated_at) : ""].filter(Boolean);
  text("brief-provenance", briefMeta.join(" · "));
  text("one-liner", brief.one_liner);
  text("what-it-is", brief.what_it_is);
  text("problem", brief.problem);
  text("core-idea", brief.core_idea);
  text("supporting-quote", brief.supporting_quote);
  text("mechanism", brief.mechanism);
  text("evidence", brief.evidence);
  text("engineering-relevance", brief.engineering_relevance);
  text("reading-guide", brief.reading_guide);
  text("limitations", brief.limitations);
  const evidenceLabels = {
    official_fulltext: "官方网页正文（专业模型整理；未核验 PDF 全文）",
    official_excerpt: "官方网页正文节选（专业模型仅基于节选整理；未核验 PDF 全文）",
    official_abstract: "官方摘要或官方元数据（专业模型整理）",
    source_summary: "来源摘要 / 正文摘录（专业模型整理）",
    none: "仅题名与元数据；不推断论文结论"
  };
  text("evidence-level", evidenceLabels[brief.evidence_level] || brief.evidence_level);
  text("raw-summary", data.original_summary, "来源没有提供摘要；专业简报不会据此虚构内容。");
  const layers = byId("layers"); clear(layers);
  const layerValues = Array.isArray(brief.system_layers) ? brief.system_layers : [];
  if (!layerValues.length) { const span = document.createElement("span"); span.textContent = "尚不能可靠定位"; layers.appendChild(span); }
  layerValues.forEach((value) => { const span = document.createElement("span"); span.className = "chip"; span.textContent = value; layers.appendChild(span); });
  const original = safeHttpUrl(data.original_url);
  if (original) { byId("original-link").href = original; byId("original-link").classList.remove("hidden"); }
  const sources = byId("sources"); clear(sources);
  (data.sources || []).forEach((source) => addListText(sources, source.name, source.category, source.source_url || source.homepage));
  if (!sources.children.length) addListText(sources, "来源信息暂无记录", "", "");
  const events = byId("events"); clear(events);
  let chosen = null;
  (data.events || []).forEach((event) => {
    const selected = selectedEvent && (String(event.run_id) === selectedEvent || event.event_id === selectedEvent);
    if (selected) chosen = event;
    const label = event.event_type === "updated" ? "实质更新" : event.event_type === "new" ? "新增" : (event.event_type || "事件");
    addListText(events, (selected ? "当前事件 · " : "") + label, [dateText(event.created_at), event.source_name].filter(Boolean).join(" · "), "");
  });
  text("selected-event", chosen ? "你从“" + (chosen.event_type === "updated" ? "更新" : "新增") + "”通知进入；事件时间：" + dateText(chosen.created_at) : "当前展示这条资料的最新整理结果。版本历史如下。");
  const versions = byId("versions"); clear(versions);
  (data.versions || []).forEach((version, index) => addListText(versions, "版本 " + (index + 1) + " · " + (version.source_name || "未知来源"), dateText(version.captured_at), version.url));
  if (!versions.children.length) addListText(versions, "暂无版本快照", "", "");
  byId("loading").classList.add("hidden");
  byId("content").classList.remove("hidden");
};
const params = new URLSearchParams(window.location.search);
const publicId = params.get("id") || "";
const selectedEvent = (params.get("event") || "").slice(0, 120);
if (!/^[a-f0-9]{32}$/.test(publicId)) fail("链接缺少有效的资料编号。请返回 Radar 后重新打开。");
else {
  const shardUrl = "items/" + publicId.slice(0, 2) + "/" + publicId + ".json" + (selectedEvent ? "?event=" + encodeURIComponent(selectedEvent) : "");
  fetch(shardUrl, {headers:{"Accept":"application/json"}})
  .then((response) => { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
  .then((data) => render(data, selectedEvent))
  .catch(() => fail("这条资料尚未发布或网络暂时不可用，请稍后重试。"));
}
</script>
</body>
</html>
""".replace("__HOME__", home_url)


def export_item_pages(
    connection: sqlite3.Connection,
    site_dir: Path | str,
    base_url: str,
) -> Dict[str, Any]:
    """Write ``item.html`` and one sharded JSON document per database item.

    The returned ``by_item_id`` mapping lets RSS/feed builders reuse exactly
    the same stable URL without querying generated files.
    """

    destination = Path(site_dir)
    base = _normalised_base_url(base_url)
    items = _rows(connection, "SELECT * FROM items ORDER BY id")
    briefs = _brief_rows(connection)

    sources = _group_by(
        _rows(
            connection,
            """
            SELECT x.item_id,x.source_id,x.external_id,x.source_url,x.first_seen_at,x.last_seen_at,
                   s.name AS source_name,s.category,s.homepage
            FROM item_sources x LEFT JOIN sources s ON s.id=x.source_id
            ORDER BY x.item_id,s.name,x.source_id,x.external_id
            """,
        ),
        "item_id",
    )
    versions = _group_by(
        _rows(
            connection,
            """
            SELECT v.item_id,v.source_id,v.captured_at,v.title,v.url,v.published_at,
                   s.name AS source_name
            FROM item_versions v LEFT JOIN sources s ON s.id=v.source_id
            ORDER BY v.item_id,v.captured_at DESC,v.source_id
            """,
        ),
        "item_id",
    )
    event_columns = _table_columns(connection, "run_events")
    optional_events = [
        column for column in ("delivered_at", "websub_published_at") if column in event_columns
    ]
    event_select = ",".join(f"e.{column}" for column in optional_events)
    if event_select:
        event_select = "," + event_select
    events = _group_by(
        _rows(
            connection,
            f"""
            SELECT e.item_id,e.run_id,e.source_id,e.event_type,e.created_at{event_select},
                   s.name AS source_name
            FROM run_events e LEFT JOIN sources s ON s.id=e.source_id
            ORDER BY e.item_id,e.created_at DESC,e.run_id DESC
            """,
        ),
        "item_id",
    )

    by_item_id: Dict[int, Dict[str, str]] = {}
    for item in items:
        item_id = int(item["id"])
        public_id = public_item_id(str(item["canonical_key"]))
        page_url = item_page_url(str(item["canonical_key"]), base)
        original_url = _safe_http_url(item.get("url"))
        if not original_url:
            for source in sources.get(item_id, []):
                original_url = _safe_http_url(source.get("source_url"))
                if original_url:
                    break
        payload = {
            "schema_version": 1,
            "public_id": public_id,
            "page_url": page_url,
            "item_type": _string(item.get("item_type")),
            "title": _string(item.get("title")),
            "original_url": original_url,
            "doi": _string(item.get("doi")),
            "authors": _string(item.get("authors")),
            "venue": _string(item.get("venue")),
            "published_at": _string(item.get("published_at")),
            "discovered_at": _string(item.get("discovered_at")),
            "updated_at": _string(item.get("updated_at")),
            "topics": _topics(item.get("topics_json")),
            "brief": _brief_payload(briefs.get(item_id)),
            "original_summary": _string(item.get("summary")),
            "sources": [_source_payload(row) for row in sources.get(item_id, [])],
            "versions": [_version_payload(row) for row in versions.get(item_id, [])],
            "events": [_event_payload(row) for row in events.get(item_id, [])],
        }
        shard_path = destination / "items" / public_id[:2] / f"{public_id}.json"
        _atomic_write_text(
            shard_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        by_item_id[item_id] = {"public_id": public_id, "url": page_url}

    _atomic_write_text(destination / "item.html", _item_html(base))
    return {
        "item_count": len(items),
        "page": str(destination / "item.html"),
        "by_item_id": by_item_id,
    }


__all__ = ["export_item_pages", "item_page_url", "public_item_id"]
