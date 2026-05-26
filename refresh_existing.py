#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按当前最新规则重排 OneNote 现有页面。

流程：
  列出分区所有页 → 抓每页 HTML → 解析"原文"URL → 用 daily_push.push_one
  重新推一份 → 删旧页 → 进度写到本地 refresh_state.json（中断可续）

跑法（本地）：

    cd ~/Desktop/hexun-onenote-rss
    git pull
    caffeinate -is python3 refresh_existing.py [--limit N] [--source 关键字] [--since YYYY-MM-DD]

参数：
  --limit N        最多处理 N 个页面（用于试水）
  --source 关键字  只重排标题包含该关键字的页（用于精选）
  --since DATE     只重排日期前缀 >= DATE 的页（titles 以 YYMMDD 开头）
  --dry-run        只列出，不真的删/推
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
from datetime import date, datetime, timedelta, timezone

import body_xhtml
import daily_push
import hexun_lib
import onenote
import rss_lib


BEIJING = timezone(timedelta(hours=8))
SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")
REFRESH_STATE = "refresh_state.json"

URL_IN_BODY_RE = re.compile(
    r'(https?://(?:insurance\.hexun\.com|mp\.weixin\.qq\.com)/[^\s"<>\']+)',
    re.I,
)
BIZ_RE = re.compile(r'__biz=([^&]+)')


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}\n请先 python3 setup.py")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_section_pages(access_token, section_id):
    """列出分区下所有页面元数据。"""
    pages = []
    url = (f"{onenote.GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
           f"?$top=100&$select=id,title,createdDateTime")
    while url:
        data = onenote._graph_get(access_token, url)
        pages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return pages


def get_page_content(access_token, page_id):
    req = urllib.request.Request(f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}/content")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def delete_page(access_token, page_id):
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}", method="DELETE",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    urllib.request.urlopen(req, timeout=30).read()


def extract_orig_url(html):
    """从 OneNote 页 HTML 抽出原 URL。优先匹配"原文："锚点，回退到任意 hexun/微信 URL。"""
    m = re.search(r'原文[：:][^<]*?<a[^>]+href="([^"]+)"', html, re.S)
    if m:
        return m.group(1)
    # 回退
    m = URL_IN_BODY_RE.search(html)
    if m:
        return m.group(1)
    return None


def build_biz_to_source_map(log):
    """从 14 个 RSS feed 拉一遍，构建 __biz → 公众号名 的映射。"""
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
    log(f"  构建完成：{len(biz_map)} 个公众号")
    return biz_map


def load_refresh_state():
    if not os.path.exists(REFRESH_STATE):
        return {"done_page_ids": []}
    try:
        with open(REFRESH_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done_page_ids": []}


def save_refresh_state(state):
    state["updated_at"] = datetime.now(BEIJING).isoformat()
    with open(REFRESH_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def refresh_one_page(access_token, section_id, page, biz_map, dry_run, log):
    """重排单页。返回 'ok'/'skip'/'fail'。"""
    page_id = page["id"]
    title = page.get("title", "")
    log(f"→ {title[:55]}")

    try:
        html = get_page_content(access_token, page_id)
    except Exception as e:
        log(f"  ! 拉取旧页内容失败：{e}")
        return "fail"

    orig_url = extract_orig_url(html)
    if not orig_url:
        log(f"  ! 找不到原 URL（可能非本工具推的页），跳过")
        return "skip"

    # 推路径：hexun 或 WeChat
    is_hexun = "insurance.hexun.com" in orig_url
    is_wx = "mp.weixin.qq.com" in orig_url
    if not (is_hexun or is_wx):
        log(f"  ! 未知 URL 类型：{orig_url[:80]}")
        return "skip"

    if dry_run:
        log(f"  [dry-run] 会重排：{orig_url[:80]}")
        return "ok"

    try:
        if is_hexun:
            # 从 URL 提取日期
            m = re.search(r'/(\d{4})-(\d{2})-(\d{2})/', orig_url)
            if not m:
                log(f"  ! URL 日期不可解析：{orig_url}")
                return "skip"
            dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            list_title = re.sub(r'^\d{6}', '', title)  # 剥掉 YYMMDD 前缀
            daily_push.push_one(
                access_token, section_id, dt, orig_url, list_title, log,
            )
        else:
            # WeChat：拉 HTML + parse_wechat_article
            wx_html = rss_lib.fetch_html(orig_url)
            wx_title, wx_dt, wx_content = rss_lib.parse_wechat_article(wx_html)
            if not wx_content or not wx_dt:
                log(f"  ! WeChat 解析失败 ({orig_url[:60]})")
                return "fail"
            # 用 __biz 查源
            bm = BIZ_RE.search(orig_url)
            src_label = biz_map.get(bm.group(1), "") if bm else ""
            list_title = re.sub(r'^\d{6}', '', title)
            daily_push.push_one(
                access_token, section_id, wx_dt.date(),
                orig_url, wx_title or list_title, log,
                prefetched_content=wx_content, source_label=src_label,
            )
    except Exception:
        log(f"  ! 重新推送失败：{traceback.format_exc().splitlines()[-1]}")
        return "fail"

    # 新页推成功，删旧
    try:
        delete_page(access_token, page_id)
    except Exception as e:
        log(f"  ! 旧页删除失败（新页已推，OneNote 里会有重复）：{e}")
    return "ok"


def main():
    ap = argparse.ArgumentParser(description="按最新规则重排 OneNote 现有页面")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个（0=全部）")
    ap.add_argument("--source", default="", help="只处理标题含该关键字的页")
    ap.add_argument("--since", default="", help="只处理 YYMMDD 前缀 >= 该日期的页 (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="不真的删/推，只列出")
    args = ap.parse_args()

    print("=" * 64)
    print("OneNote 现有页面重排（按最新规则）")
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
    pages = list_section_pages(access_token, section_id)
    log(f"  共 {len(pages)} 页")

    # 过滤
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

    # 排除已 refresh 过的
    state = load_refresh_state()
    done = set(state.get("done_page_ids", []))
    pending = [p for p in pages if p["id"] not in done]
    log(f"  已 refresh 过 {len(done)} 个，待处理 {len(pending)} 个")

    if args.limit > 0:
        pending = pending[:args.limit]
        log(f"  --limit 截取后：{len(pending)}")

    if not pending:
        log("没有待处理页面。")
        return

    if not args.dry_run:
        est_min = len(pending) * 12 / 60
        log(f"\n预计耗时 ~{est_min:.0f} 分钟（按 12 秒/页）")
        ans = input("继续？(Y/n)：").strip().lower()
        if ans == "n":
            return

    biz_map = build_biz_to_source_map(log) if not args.dry_run else {}

    n_ok = n_skip = n_fail = 0
    for i, page in enumerate(pending, 1):
        log(f"\n[{i}/{len(pending)}]")
        try:
            result = refresh_one_page(access_token, section_id, page, biz_map, args.dry_run, log)
        except Exception:
            log(f"  ! 异常：{traceback.format_exc().splitlines()[-1]}")
            result = "fail"

        if result == "ok":
            n_ok += 1
            if not args.dry_run:
                done.add(page["id"])
        elif result == "skip":
            n_skip += 1
            done.add(page["id"])  # 跳过的也记上，避免下次重试
        else:
            n_fail += 1

        if (i % 10) == 0 and not args.dry_run:
            state["done_page_ids"] = sorted(done)
            save_refresh_state(state)
            log(f"  ✓ 进度保存（OK {n_ok} / 跳过 {n_skip} / 失败 {n_fail}）")
        time.sleep(1)

    if not args.dry_run:
        state["done_page_ids"] = sorted(done)
        save_refresh_state(state)
    log("\n" + "=" * 64)
    log(f"完成。重排 {n_ok}，跳过 {n_skip}，失败 {n_fail}")
    log("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断（每 10 页保存一次进度，再跑会接着处理剩下的）")
