#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地一键发一篇"样式测试页"到 OneNote，几秒内验证字号/字体/栏宽是否生效。

跑法：
    cd ~/Desktop/hexun-onenote-rss
    git pull
    python3 test_style.py

会推 3 个版本到 OneNote 同一分区：
    A. 默认（OneNote 自己的默认样式，作为对照基准）
    B. 当前 onenote.py 的样式
    C. 故意夸张样式（红色 24pt 楷体 1400px）—— 用来确认 OneNote 到底听不听 CSS
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import onenote

BEIJING = timezone(timedelta(hours=8))
SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")


def main():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}，先跑 python3 setup.py")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        secrets = json.load(f)

    access_token, _ = onenote.refresh_access_token(
        secrets["AZURE_CLIENT_ID"], secrets["MS_REFRESH_TOKEN"]
    )
    section_id = secrets["ONENOTE_SECTION_ID"]
    now = datetime.now(BEIJING)
    ts = now.strftime("%y%m%d %H:%M")

    long_para = (
        "这是一段足够长的测试段落，用来观察每行能容纳多少汉字。"
        "如果栏宽真的从默认的 600 像素加宽到了 1125 像素（多 87%），"
        "那么本段你会看到明显比默认更长的行。"
        "字体应是宋体（serif，带衬线），字号应明显比 OneNote 默认大一号到两号。"
        "重复一遍：宋体 14pt 1125px。"
    ) * 3

    # ---- A. 默认（不动 create_page）----
    body_a = (
        "<p><b>测试 A：基础对照（无定制样式）</b></p>"
        "<p>" + long_para + "</p>"
    )
    # 临时绕开 create_page 的样式注入，直接发原始
    # 简单办法：直接用 multipart 发，跳过样式逻辑
    import secrets as _s
    import urllib.request, urllib.error
    boundary = "----TestB" + _s.token_hex(8)
    html_a = (
        '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml">'
        f'<head><title>{ts} 样式测试 A 基础</title></head>'
        f'<body><p>测试 A：OneNote 默认样式（无任何 inline CSS）</p>'
        f'<p>{long_para}</p></body></html>'
    ).encode("utf-8")
    body_bytes = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="Presentation"\r\n'
        "Content-Type: application/xhtml+xml\r\n\r\n"
    ).encode() + html_a + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/sections/{section_id}/pages",
        data=body_bytes, method="POST",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("A 推送成功:", r.status)
    except urllib.error.HTTPError as e:
        print("A 推送失败:", e.code, e.read()[:200])

    # ---- B. 当前生产样式 ----
    body_b = (
        "<p><b>测试 B：生产样式（当前 onenote.py）</b></p>"
        f"<p>{long_para}</p>"
        f"<p>{long_para}</p>"
    )
    try:
        onenote.create_page(
            access_token, section_id,
            f"{ts} 样式测试 B 生产",
            body_b, [],
            created_iso=now.isoformat(),
        )
        print("B 推送成功（用 create_page）")
    except Exception as e:
        print("B 推送失败:", e)

    # ---- C. 夸张样式（红 24pt 楷体 1400px）----
    extreme_outline = "position:absolute;left:48px;top:120px;width:1400px;"
    extreme_elem = "font-family:'KaiTi';font-size:24.0pt;color:#e74c3c"
    html_c = (
        '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml">'
        f'<head><title>{ts} 样式测试 C 夸张</title></head>'
        '<body>'
        f'<div data-id="test-outline" style="{extreme_outline}">'
        f'<p style="{extreme_elem}"><span style="{extreme_elem}">'
        f'测试 C：红色 24pt 楷体 1400px 栏宽。如果这条都不变样，那 OneNote 确实在吞我的 CSS。'
        f'</span></p>'
        f'<p style="{extreme_elem}"><span style="{extreme_elem}">{long_para}</span></p>'
        '</div></body></html>'
    ).encode("utf-8")
    boundary_c = "----TestC" + _s.token_hex(8)
    body_bytes = (
        f"--{boundary_c}\r\n"
        'Content-Disposition: form-data; name="Presentation"\r\n'
        "Content-Type: application/xhtml+xml\r\n\r\n"
    ).encode() + html_c + f"\r\n--{boundary_c}--\r\n".encode()
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/sections/{section_id}/pages",
        data=body_bytes, method="POST",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary_c}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("C 推送成功:", r.status)
    except urllib.error.HTTPError as e:
        print("C 推送失败:", e.code, e.read()[:200])

    print()
    print("=" * 60)
    print("3 条都推完了。去 OneNote 该分区找最新 3 篇（标题以日期开头）。")
    print("对比观察：")
    print("  - A vs B：如果 B 不比 A 宽/大/换字体，说明 onenote.py 的样式失效")
    print("  - C：如果连红色 24pt 楷体 1400px 都不变样，那 OneNote 完全在吞 CSS（重大警报）")
    print("=" * 60)


if __name__ == "__main__":
    main()
