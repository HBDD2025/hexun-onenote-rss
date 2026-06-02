#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读 feed_items.json，生成 docs/feed.xml（RSS 2.0）。

被 GitHub Actions workflow 在 daily_push 之后调用。生成的 feed.xml 通过
GitHub Pages 发布，URL 形如：
  https://hbdd2025.github.io/hexun-onenote-rss/feed.xml

也顺便生成 docs/index.html 当落地页，方便人肉浏览。
"""

import email.utils
import html as _html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta


BEIJING = timezone(timedelta(hours=8))

FEED_FILE = os.environ.get("FEED_ITEMS_FILE", "feed_items.json")
OUT_DIR = "docs"
OUT_FEED = os.path.join(OUT_DIR, "feed.xml")
OUT_INDEX = os.path.join(OUT_DIR, "index.html")
OUT_OPDS = os.path.join(OUT_DIR, "opds.xml")
OUT_DIGEST = os.path.join(OUT_DIR, "digest.xml")
EPUB_FILENAME = "kindle-latest.epub"

DIGEST_CHANNEL_TITLE = "AI 推送（合订本）"
DIGEST_LINK = "https://hbdd2025.github.io/hexun-onenote-rss/"

# Channel meta
CHANNEL_TITLE = "保险行业聚合（和讯 + 公众号）"
CHANNEL_LINK = "https://hbdd2025.github.io/hexun-onenote-rss/"
CHANNEL_DESC = "和讯保险 5 个栏目 + 14 个公众号 RSS 聚合，每日 3 次更新"


def _rfc822(iso_str):
    """ISO8601 → RFC822（RSS pubDate 标准格式）。"""
    if not iso_str:
        return email.utils.format_datetime(datetime.now(BEIJING))
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return email.utils.format_datetime(datetime.now(BEIJING))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING)
    return email.utils.format_datetime(dt)


def _x_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr_escape(s):
    return (s or "").replace("&", "&amp;").replace('"', "&quot;") \
                    .replace("<", "&lt;").replace(">", "&gt;")


def _plain_summary(html_str, limit=240):
    """从 HTML 提取纯文本前 N 字作为 description（短摘要）。"""
    if not html_str:
        return ""
    t = re.sub(r"<[^>]+>", "", html_str)
    # 把所有 HTML / XML 实体解码成真字符 (&#160; → '\xa0', &amp; → '&', 等)
    t = _html.unescape(t)
    # \xa0 / 多空白压成单个空格
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def build_rss(items):
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0" '
                 'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
                 'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                 'xmlns:atom="http://www.w3.org/2005/Atom">')
    parts.append('<channel>')
    parts.append(f'  <title>{_x_escape(CHANNEL_TITLE)}</title>')
    parts.append(f'  <link>{_x_escape(CHANNEL_LINK)}</link>')
    parts.append(f'  <description>{_x_escape(CHANNEL_DESC)}</description>')
    parts.append('  <language>zh-CN</language>')
    parts.append(f'  <lastBuildDate>{_rfc822(datetime.now(BEIJING).isoformat())}</lastBuildDate>')
    parts.append(f'  <atom:link href="{_attr_escape(CHANNEL_LINK + "feed.xml")}" '
                 'rel="self" type="application/rss+xml" />')

    for it in items:
        url = it.get("url", "")
        title = it.get("title", "(无标题)")
        source = it.get("source", "")
        pubdate = it.get("pubdate_iso", "")
        content = it.get("content_html", "")
        # CDATA 中只需要防 ]]> 出现；其他不动
        safe_content = content.replace("]]>", "]]&gt;")
        summary = _plain_summary(content, 240)

        parts.append('  <item>')
        parts.append(f'    <title>{_x_escape(title)}</title>')
        parts.append(f'    <link>{_x_escape(url)}</link>')
        parts.append(f'    <guid isPermaLink="true">{_x_escape(url)}</guid>')
        parts.append(f'    <pubDate>{_rfc822(pubdate)}</pubDate>')
        if source:
            parts.append(f'    <dc:creator>{_x_escape(source)}</dc:creator>')
            parts.append(f'    <category>{_x_escape(source)}</category>')
        parts.append(f'    <description>{_x_escape(summary)}</description>')
        parts.append(f'    <content:encoded><![CDATA[{safe_content}]]></content:encoded>')
        parts.append('  </item>')

    parts.append('</channel>')
    parts.append('</rss>')
    return "\n".join(parts)


def build_digest(items, epub_mtime_iso):
    """单条目 RSS：把所有 items 拼成一个 <content:encoded>，作为一条
    "AI推送 · YYYY-MM-DD HH:MM" 的 RSS item。每次 EPUB 更新（mtime 变）
    guid 变，KOReader NewsDownloader 触发下载 → 一次同步只生成 1 本 EPUB。"""
    push_dt = datetime.fromisoformat(epub_mtime_iso)
    push_label = push_dt.strftime("%Y-%m-%d %H:%M")
    push_compact = push_dt.strftime("%Y%m%dT%H%M%S")
    title = f"AI推送 · {push_label}"
    guid = f"tag:hbdd2025.github.io,hexun-onenote-rss:digest-{push_compact}"

    # 拼正文：每条新闻一个章节，章首加大标题
    chapter_parts = []
    for it in items:
        ttl = _x_escape(it.get("title", "(无标题)"))
        src = _x_escape(it.get("source", ""))
        dt_short = _x_escape((it.get("pubdate_iso", "") or "")[:10])
        chapter_parts.append(
            f'<h2>{ttl}</h2>\n'
            f'<p style="color:#666;font-size:0.9em;">[{src}] {dt_short}</p>\n'
            + (it.get("content_html") or "")
            + '\n<hr />'
        )
    full_html = "\n".join(chapter_parts)
    safe_full = full_html.replace("]]>", "]]&gt;")

    summary = f"本期合订本含 {len(items)} 条新闻（推送时间 {push_label}）。"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{_x_escape(DIGEST_CHANNEL_TITLE)}</title>
  <link>{_x_escape(DIGEST_LINK)}</link>
  <description>每次推送一个合订本 entry，包含当时所有最新新闻。KOReader NewsDownloader 一次同步 = 一本 EPUB。</description>
  <language>zh-CN</language>
  <lastBuildDate>{_rfc822(epub_mtime_iso)}</lastBuildDate>
  <atom:link href="{_attr_escape(DIGEST_LINK + 'digest.xml')}" rel="self" type="application/rss+xml" />
  <item>
    <title>{_x_escape(title)}</title>
    <link>{_attr_escape(DIGEST_LINK + EPUB_FILENAME)}</link>
    <guid isPermaLink="false">{_x_escape(guid)}</guid>
    <pubDate>{_rfc822(epub_mtime_iso)}</pubDate>
    <dc:creator>{_x_escape(BOOK_AUTHOR)}</dc:creator>
    <description>{_x_escape(summary)}</description>
    <content:encoded><![CDATA[{safe_full}]]></content:encoded>
  </item>
</channel>
</rss>
"""


def build_opds(items, epub_filename, epub_mtime_iso, epub_size):
    """OPDS Atom catalog：让 KOReader 等 OPDS 客户端能"一键下载合订本 EPUB"。

    单条目 — 指向 kindle-latest.epub。href 用相对路径，
    OPDS 客户端会基于 catalog URL 解析（jsDelivr 或 GitHub Pages 都能用）。"""
    summary_text = (
        f"包含最近 {len(items)} 条新闻（和讯保险 5 个栏目 + 14 个公众号 RSS 聚合）。"
        f"自动更新；每次拉到的是最新合订本。"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opds="http://opds-spec.org/2010/catalog"
      xmlns:dc="http://purl.org/dc/terms/">
  <id>tag:hbdd2025.github.io,hexun-onenote-rss:opds</id>
  <title>{_x_escape(CHANNEL_TITLE)}</title>
  <updated>{epub_mtime_iso}</updated>
  <author><name>{_x_escape(BOOK_AUTHOR)}</name></author>
  <link rel="self" type="application/atom+xml;profile=opds-catalog;kind=acquisition" href="opds.xml"/>
  <link rel="start" type="application/atom+xml;profile=opds-catalog;kind=acquisition" href="opds.xml"/>
  <entry>
    <id>tag:hbdd2025.github.io,hexun-onenote-rss:latest</id>
    <title>AI推送（最新合订本）</title>
    <updated>{epub_mtime_iso}</updated>
    <author><name>{_x_escape(BOOK_AUTHOR)}</name></author>
    <dc:language>zh-CN</dc:language>
    <dc:issued>{epub_mtime_iso[:10]}</dc:issued>
    <summary>{_x_escape(summary_text)}</summary>
    <link rel="http://opds-spec.org/acquisition"
          href="{_attr_escape(epub_filename)}"
          type="application/epub+zip"
          length="{epub_size}"/>
  </entry>
</feed>
"""


BOOK_AUTHOR = "和讯保险 + 公众号"


def build_index_html(items):
    """简易落地页：列出最近 50 条，链接到原文。"""
    rows = []
    for it in items[:50]:
        title = _x_escape(it.get("title", ""))
        url = _attr_escape(it.get("url", ""))
        source = _x_escape(it.get("source", ""))
        date = _x_escape((it.get("pubdate_iso", "") or "")[:10])
        rows.append(
            f'<li><span class="d">{date}</span> '
            f'<span class="s">[{source}]</span> '
            f'<a href="{url}" target="_blank" rel="noopener">{title}</a></li>'
        )
    feed_url = CHANNEL_LINK + "feed.xml"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_x_escape(CHANNEL_TITLE)}</title>
<link rel="alternate" type="application/rss+xml" title="{_x_escape(CHANNEL_TITLE)}"
      href="{_attr_escape(feed_url)}" />
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 880px; margin: 2em auto; padding: 0 1em; line-height: 1.6;
         color: #222; }}
  h1 {{ font-size: 1.4em; margin-bottom: 0.2em; }}
  .sub {{ color: #666; font-size: 0.9em; margin-bottom: 1.5em; }}
  .rss-link {{ display: inline-block; background:#ff8800; color:#fff;
               padding:4px 10px; border-radius:4px; text-decoration:none;
               font-size:0.9em; margin-right:8px; }}
  .epub-link {{ display: inline-block; background:#2c7;color:#fff;
                padding:4px 10px; border-radius:4px; text-decoration:none;
                font-size:0.9em; margin-right:8px; }}
  .opds-link {{ display: inline-block; background:#48a;color:#fff;
                padding:4px 10px; border-radius:4px; text-decoration:none;
                font-size:0.9em; margin-right:8px; }}
  .digest-link {{ display: inline-block; background:#933;color:#fff;
                  padding:4px 10px; border-radius:4px; text-decoration:none;
                  font-size:0.9em; margin-right:8px; }}
  ul.items {{ list-style: none; padding: 0; }}
  ul.items li {{ padding: 6px 0; border-bottom: 1px solid #eee; }}
  ul.items .d {{ color:#999; font-family: monospace; font-size:0.85em;
                 margin-right:6px; }}
  ul.items .s {{ color:#0a7; font-size:0.85em; margin-right:6px; }}
  ul.items a {{ color:#226; text-decoration:none; }}
  ul.items a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<h1>{_x_escape(CHANNEL_TITLE)}</h1>
<p class="sub">
  <a class="rss-link" href="feed.xml">📡 订阅 RSS（每条新闻一项）</a>
  <a class="digest-link" href="digest.xml">📰 RSS 合订本（一次推送一项）</a>
  <a class="epub-link" href="kindle-latest.epub">📖 下载 EPUB</a>
  <a class="opds-link" href="opds.xml">📚 OPDS（KOReader 用）</a>
  共 {len(items)} 条 · 最近更新 {_x_escape((items[0].get("pubdate_iso","")[:16]) if items else "")}
</p>
<ul class="items">
{chr(10).join(rows)}
</ul>
</body>
</html>
"""


def main():
    with open(FEED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    # feed_items.json 里已经按 pubdate_iso 倒序，直接用
    os.makedirs(OUT_DIR, exist_ok=True)
    rss = build_rss(items)
    with open(OUT_FEED, "w", encoding="utf-8") as f:
        f.write(rss)
    html = build_index_html(items)
    with open(OUT_INDEX, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {OUT_FEED} ({len(items)} items, {os.path.getsize(OUT_FEED)} B)")
    print(f"wrote {OUT_INDEX}")

    # OPDS catalog + digest.xml（都需要 kindle-latest.epub 存在；EPUB 的 mtime
    # 作为本期推送时间，让 digest entry 的 guid 跟实际内容变化对齐）
    epub_path = os.path.join(OUT_DIR, EPUB_FILENAME)
    if os.path.exists(epub_path):
        epub_mtime = datetime.fromtimestamp(os.path.getmtime(epub_path), tz=BEIJING)
        epub_mtime_iso = epub_mtime.isoformat()
        epub_size = os.path.getsize(epub_path)
        opds = build_opds(items, EPUB_FILENAME, epub_mtime_iso, epub_size)
        with open(OUT_OPDS, "w", encoding="utf-8") as f:
            f.write(opds)
        print(f"wrote {OUT_OPDS} (pointing to {EPUB_FILENAME}, {epub_size} B)")

        digest = build_digest(items, epub_mtime_iso)
        with open(OUT_DIGEST, "w", encoding="utf-8") as f:
            f.write(digest)
        print(f"wrote {OUT_DIGEST} (single-entry digest of {len(items)} items)")
    else:
        print(f"skip {OUT_OPDS} / {OUT_DIGEST}（{epub_path} 不存在）")


if __name__ == "__main__":
    main()
