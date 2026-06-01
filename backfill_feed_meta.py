#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性补丁：给 feed_items.json 里**缺 meta 块**的旧条目，前置一个合成的 meta 块。

背景：早期版本的 daily_push.py 把"裸正文"存进了 feed_items.json，没带 meta。
后来 daily_push 改了，新条目自带 meta；但旧条目还是裸的，于是 RSS / Kindle
里看到这些条目就少了"推送自和讯保险的XX频道 / 来源 / 发布时间 / 推送时间"。

本脚本扫一遍 feed_items.json，对没 meta 的条目按现有 source / url / pubdate_iso
合成一份 meta 前置进去。hexun_channel 字段若存在则用，没有则该行省略（早期没存）。

用法：
    python backfill_feed_meta.py            # 改写 feed_items.json
    python backfill_feed_meta.py --dry-run  # 只看哪些会改，不写

通常本地手动跑一次即可；跑完 commit feed_items.json 即可。
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta


BEIJING = timezone(timedelta(hours=8))
FEED = os.environ.get("FEED_ITEMS_FILE", "feed_items.json")


def _x(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def has_meta(content_html):
    """启发式判断 content 里有没有我们的 meta 块。"""
    if not content_html:
        return False
    # daily_push 现在生成的 meta 一定含「<b>来源：</b>」
    return "<b>来源：</b>" in content_html


def build_meta_for(item, backfill_time_str):
    """根据 item 字段合成 meta 块。hexun_channel 字段缺失则不出"推送自和讯..."这一行。"""
    url = item.get("url", "")
    source = item.get("source", "")
    pubdate_iso = item.get("pubdate_iso", "")
    publish_str = pubdate_iso[:10] if pubdate_iso else ""
    channel = item.get("hexun_channel", "")

    channel_line = ""
    if channel:
        channel_line = f'<p><b>推送自和讯保险的「{_x(channel)}」频道</b></p>'

    return (
        channel_line
        + f'<p><b>来源：</b>{_x(source)}'
        f' &nbsp;|&nbsp; '
        f'<a href="{_x(url)}">原文链接</a></p>'
        f'<p><b>发布时间：</b>{_x(publish_str)}'
        f' &nbsp;|&nbsp; '
        f'<b>推送时间：</b>{backfill_time_str}（回填）</p>'
        f'<hr />'
    )


def main():
    dry_run = "--dry-run" in sys.argv

    with open(FEED, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    backfill_time = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    patched = 0
    skipped = 0
    for it in items:
        c = it.get("content_html", "")
        if has_meta(c):
            skipped += 1
            continue
        new_content = build_meta_for(it, backfill_time) + c
        if dry_run:
            print(f"  WOULD PATCH: [{it.get('source','')}] {it.get('title','')[:50]}")
        else:
            it["content_html"] = new_content
        patched += 1

    print(f"已补 meta 的条目: {patched}  /  已有 meta 跳过: {skipped}  /  总数: {len(items)}")

    if not dry_run and patched:
        with open(FEED, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已写回 {FEED}")


if __name__ == "__main__":
    main()
