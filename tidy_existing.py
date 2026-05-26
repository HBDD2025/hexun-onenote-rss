#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原地规整 OneNote 现有页面：仅对页面里现存 HTML 应用最新清理规则，不重新从源拉。

会做的：
  - 剥编辑/责编/排版/版权声明/长按二维码 等末尾签名
  - 应用通用 文章原文+3行段落 规则
  - 应用各公众号的源特定规则（首图、来源后图、文章原文前图 等）
  - 重新注入字体字号（14pt 宋体）
  - PATCH 替换 body 内容（不删页，page id 不变）

⚠️ 限制：PATCH API 不支持 multipart，原有图片（OneNote 资源链接）在 PATCH 后
会失效（显示红叉）。所以本工具**把所有图片替换成"本处有图片"占位**——失去图，
但避免红叉。要保住图就用 refresh_existing.py（删页 + 重抓源）。

不会做的：
  - 不重新拉取原文（如果你需要从源完全重新生成，用 refresh_existing.py）
  - 不保留图片（见上面 ⚠️）
  - 不调整 outline 宽度/位置（OneNote 一向不认）

跑法（本地）：

    cd ~/Desktop/hexun-onenote-rss
    git pull
    python3 tidy_existing.py [--limit N] [--source 关键字] [--since YYYY-MM-DD] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import body_xhtml
import daily_push
import onenote
import rss_lib


BEIJING = timezone(timedelta(hours=8))
SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")
STATE_FILE = "tidy_state.json"

URL_IN_BODY_RE = re.compile(
    r'(https?://(?:insurance\.hexun\.com|mp\.weixin\.qq\.com)/[^\s"<>\']+)',
    re.I,
)
BIZ_RE = re.compile(r'__biz=([^&]+)')
ALL_IMG_RE = re.compile(r'<img\s[^>]*/?>', re.I)
PLACEHOLDER_TEXT = '<p>【本处有图片，但未能获取】</p>'


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}, 先 python3 setup.py")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_pages(access_token, section_id):
    pages = []
    url = (f"{onenote.GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
           f"?$top=100&$select=id,title,createdDateTime")
    while url:
        data = onenote._graph_get(access_token, url)
        pages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return pages


def get_page_content(access_token, page_id, include_ids=True):
    suffix = "?includeIDs=true" if include_ids else ""
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}/content{suffix}"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def patch_page(access_token, page_id, actions):
    """actions: list of dicts {target, action, content}"""
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}/content",
        data=json.dumps(actions).encode("utf-8"),
        method="PATCH",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH HTTP {e.code}: {body[:400]}")


def extract_orig_url(html):
    m = re.search(r'原文[：:][^<]*?<a[^>]+href="([^"]+)"', html, re.S)
    if m:
        return m.group(1)
    m = URL_IN_BODY_RE.search(html)
    if m:
        return m.group(1)
    return None


def identify_source(url, biz_map):
    if not url:
        return ""
    if "insurance.hexun.com" in url:
        return "和讯"
    bm = BIZ_RE.search(url)
    if bm:
        return biz_map.get(bm.group(1), "")
    return ""


def extract_body_inner(html):
    """提取 <body>...</body> 之间的 HTML 内容。"""
    m = re.search(r'<body[^>]*>(.*?)</body>', html, re.S)
    return m.group(1) if m else None


def apply_in_place_source_rules(html, source_label):
    """在 OneNote HTML 上应用源特定规则。

    重要：tidy 模式下，所有现存 <img>（src 是 graph.microsoft.com 资源链接）
    在 PATCH 后都会失效显示红叉。所以**所有源**都先把所有图替换成占位文字，
    然后才应用源特定的规则（首图/锚点删除等）。"""
    # 先把所有 graph.microsoft.com 的 img（PATCH 后必失效）换成占位
    html = re.sub(
        r'<img\s[^>]*?\bsrc="https://graph\.microsoft\.com/[^"]+"[^>]*/?>',
        PLACEHOLDER_TEXT, html,
    )

    if not source_label:
        return html

    # 中国保险学会：保险起见，再来一遍兜底全清
    if "中国保险学会" in source_label:
        return ALL_IMG_RE.sub(PLACEHOLDER_TEXT, html)

    # 慧保天下：删 [开头到首图（含）] + [首图后第一段]
    if "慧保天下" in source_label:
        m = ALL_IMG_RE.search(html)
        if m:
            after = html[m.end():]
            after = re.sub(
                r'^\s*(?:<br\s*/?>\s*)*<p[^>]*>.*?</p>\s*',
                '', after, count=1, flags=re.S,
            )
            html = after
    elif any(s in source_label for s in ("中国银行保险报", "今日保", "保契", "13个精算师")):
        # 简单首图删除
        m = ALL_IMG_RE.search(html)
        if m:
            # 看首图是否独占一个 <p>
            p_match = re.search(
                r'<p[^>]*>\s*' + re.escape(html[m.start():m.end()]) + r'\s*</p>',
                html, re.S,
            )
            if p_match:
                html = html[:p_match.start()] + html[p_match.end():]
            else:
                html = html[:m.start()] + html[m.end():]

    # 文本锚点规则
    rules = [
        ("中国银行保险报", "来源：", "imgs_after"),
        ("中国银行保险报", "来源:", "imgs_after"),
        ("保观",          "保观 | 聚焦保险创新", "next_img"),
        ("保险一哥",       "文章原文", "prev_img"),
    ]
    for src_kw, anchor, action in rules:
        if src_kw not in source_label:
            continue
        idx = html.find(anchor)
        if idx < 0:
            continue
        if action == "imgs_after":
            head = html[:idx + len(anchor)]
            tail = html[idx + len(anchor):]
            tail = ALL_IMG_RE.sub(PLACEHOLDER_TEXT, tail)
            html = head + tail
        elif action == "next_img":
            head = html[:idx + len(anchor)]
            tail = html[idx + len(anchor):]
            tail = ALL_IMG_RE.sub('', tail, count=1)
            html = head + tail
        elif action == "prev_img":
            before = html[:idx]
            after = html[idx:]
            last_img = None
            for m in ALL_IMG_RE.finditer(before):
                last_img = m
            if last_img:
                html = before[:last_img.start()] + before[last_img.end():] + after
    return html


def build_biz_map(log):
    log("构建 __biz → 公众号名 映射...")
    biz_map = {}
    for feed_url in rss_lib.FEEDS:
        try:
            chan_title, items = rss_lib.parse_feed(feed_url)
            for it in items:
                link = it.get("link", "")
                bm = BIZ_RE.search(link)
                if bm:
                    biz_map[bm.group(1)] = chan_title
        except Exception as e:
            log(f"  ! feed 失败：{e}")
    log(f"  映射完成 ({len(biz_map)} 个公众号)")
    return biz_map


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"done_page_ids": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done_page_ids": []}


def save_state(state):
    state["updated_at"] = datetime.now(BEIJING).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tidy_one_page(access_token, page, biz_map, dry_run, log):
    page_id = page["id"]
    title = page.get("title", "")
    log(f"→ {title[:55]}")

    try:
        html = get_page_content(access_token, page_id, include_ids=False)
    except Exception as e:
        log(f"  ! GET 失败：{e}")
        return "fail"

    orig_url = extract_orig_url(html)
    source_label = identify_source(orig_url, biz_map)
    log(f"  源：{source_label or '(未知)'}, URL: {(orig_url or '')[:80]}")

    body_inner = extract_body_inner(html)
    if not body_inner:
        log(f"  ! 没找到 <body>，跳过")
        return "skip"

    # 应用所有规则
    new_body = body_xhtml._strip_promo(body_inner)
    new_body = apply_in_place_source_rules(new_body, source_label)
    # 重新注入字号字体
    element_style = (
        f"font-family:'{onenote.PAGE_FONT_FAMILY}';"
        f"font-size:{onenote.PAGE_FONT_SIZE_PT}.0pt"
    )
    new_body = onenote._inject_inline_style(new_body, element_style)
    new_body = onenote._wrap_text_in_span(new_body, element_style)

    if new_body.strip() == body_inner.strip():
        log(f"  · 无变化，跳过")
        return "skip"

    if dry_run:
        log(f"  [dry-run] 会 PATCH replace target=body")
        log(f"           原长 {len(body_inner)} → 新长 {len(new_body)}")
        return "ok"

    try:
        patch_page(access_token, page_id, [{
            "target": "body",
            "action": "replace",
            "content": new_body,
        }])
        log(f"  ✓ 已原地更新 ({len(body_inner)} → {len(new_body)} 字)")
    except Exception as e:
        log(f"  ! PATCH 失败：{e}")
        return "fail"
    return "ok"


def main():
    ap = argparse.ArgumentParser(description="原地规整 OneNote 现有页面")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source", default="")
    ap.add_argument("--since", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print("OneNote 原地规整")
    print("=" * 64)

    secrets = load_secrets()
    log("刷新 access_token...")
    access_token, new_refresh = onenote.refresh_access_token(
        secrets["AZURE_CLIENT_ID"], secrets["MS_REFRESH_TOKEN"]
    )
    section_id = secrets["ONENOTE_SECTION_ID"]
    if new_refresh != secrets["MS_REFRESH_TOKEN"]:
        secrets["MS_REFRESH_TOKEN"] = new_refresh
        with open(SECRETS_PATH, "w", encoding="utf-8") as f:
            json.dump(secrets, f, indent=2, ensure_ascii=False)
        log("refresh_token 已更新本地副本")

    log("列出分区下所有页面...")
    pages = list_pages(access_token, section_id)
    log(f"  共 {len(pages)} 页")

    if args.source:
        pages = [p for p in pages if args.source in p.get("title", "")]
        log(f"  按 source 过滤后：{len(pages)}")
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").date()
            since_prefix = since_dt.strftime("%y%m%d")
            pages = [p for p in pages if (p.get("title", "")[:6] or "000000") >= since_prefix]
            log(f"  按 since 过滤后：{len(pages)}")
        except ValueError:
            print(f"--since 日期格式错")
            sys.exit(1)

    state = load_state()
    done = set(state.get("done_page_ids", []))
    pending = [p for p in pages if p["id"] not in done]
    log(f"  已 tidy 过 {len(done)}，待处理 {len(pending)}")

    if args.limit > 0:
        pending = pending[:args.limit]
        log(f"  --limit 截取后：{len(pending)}")

    if not pending:
        log("没有待处理页面。")
        return

    if not args.dry_run:
        est_min = len(pending) * 4 / 60
        log(f"\n预计耗时 ~{est_min:.0f} 分钟（按 4 秒/页，比 refresh_existing 快得多）")
        ans = input("继续？(Y/n)：").strip().lower()
        if ans == "n":
            return

    biz_map = build_biz_map(log) if not args.dry_run else {}

    n_ok = n_skip = n_fail = 0
    for i, page in enumerate(pending, 1):
        log(f"\n[{i}/{len(pending)}]")
        try:
            result = tidy_one_page(access_token, page, biz_map, args.dry_run, log)
        except Exception:
            log(f"  ! 异常：{traceback.format_exc().splitlines()[-1]}")
            result = "fail"
        if result == "ok":
            n_ok += 1
            if not args.dry_run:
                done.add(page["id"])
        elif result == "skip":
            n_skip += 1
            done.add(page["id"])
        else:
            n_fail += 1

        if (i % 20) == 0 and not args.dry_run:
            state["done_page_ids"] = sorted(done)
            save_state(state)
            log(f"  ✓ 进度保存（OK {n_ok} / 跳过 {n_skip} / 失败 {n_fail}）")
        time.sleep(0.5)

    if not args.dry_run:
        state["done_page_ids"] = sorted(done)
        save_state(state)
    log("\n" + "=" * 64)
    log(f"完成。原地更新 {n_ok}，跳过 {n_skip}，失败 {n_fail}")
    log("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断（每 20 页保存一次进度，再跑会接着处理剩下的）")
