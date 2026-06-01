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


def _plain_summary(html, limit=240):
    """从 HTML 提取纯文本前 N 字作为 description（短摘要）。"""
    if not html:
        return ""
    t = re.sub(r"<[^>]+>", "", html)
    t = re.sub(r"&nbsp;|\xa0", " ", t)
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
  <a class="rss-link" href="feed.xml">📡 订阅 RSS</a>
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


if __name__ == "__main__":
    main()
