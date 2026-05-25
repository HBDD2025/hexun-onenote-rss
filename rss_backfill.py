#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
14 个公众号历史备查（需要 jintiankansha VIP）。

工作流程：
  jintiankansha 专栏 listing  → 每篇 /t/XXX
                          ↓ /t_original/XXX + VIP cookie
                          → 302 → 真 mp.weixin.qq.com URL
                          → 抓 WeChat 文章页（标题/日期/正文/图）
                          → 推 OneNote

跑法（本地，必须 Actions 已 disable，且不要让 Mac 睡眠）：

    cd ~/Desktop/hexun-onenote-rss
    caffeinate -is python3 rss_backfill.py 2026-01-01

VIP cookie 放在 ~/jintiankansha-cookies.txt（一行，整个 Cookie 头的值）。
"""

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import body_xhtml
import daily_push
import onenote
import rss_lib


BEIJING = timezone(timedelta(hours=8))
SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")
COOKIE_PATH = os.path.expanduser("~/jintiankansha-cookies.txt")
STATE_PATH = "state.json"
DELAY_LISTING = (0.5, 1.2)
DELAY_ARTICLE = (1.5, 3.0)
MAX_LISTING_PAGES = 25
STATE_SAVE_EVERY = 20


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}\n请先在本地跑 python3 setup.py")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cookie():
    if not os.path.exists(COOKIE_PATH):
        print(f"找不到 {COOKIE_PATH}")
        print("请把 jintiankansha 的 Cookie 头放到这个文件里（一行）。")
        sys.exit(1)
    with open(COOKIE_PATH, "r", encoding="utf-8") as f:
        c = f.read().strip()
    if not c or "=" not in c:
        print(f"{COOKIE_PATH} 内容看起来不对（应是 'k1=v1; k2=v2; ...'）")
        sys.exit(1)
    return c


_LINK_RE = re.compile(
    r'<a[^>]+target="_blank"\s+href="(http://www\.jintiankansha\.me/t/[^"]+)"[^>]*>([^<]+)</a>',
    re.I,
)


def walk_column_listing(column_url, log):
    """走某专栏所有 ?page=N，yield (article_t_url, title)。"""
    seen = set()
    consec_empty = 0
    for page in range(1, MAX_LISTING_PAGES + 1):
        sep = "&" if "?" in column_url else "?"
        url = f"{column_url}{sep}page={page}"
        try:
            text = rss_lib.fetch_html(url)
        except Exception as e:
            log(f"    ! page={page} 失败：{e}")
            consec_empty += 1
            if consec_empty >= 2:
                break
            time.sleep(random.uniform(*DELAY_LISTING))
            continue
        links = _LINK_RE.findall(text)
        new = [(u, t.strip()) for u, t in links if u not in seen]
        if not new:
            consec_empty += 1
            if consec_empty >= 2:
                break
            time.sleep(random.uniform(*DELAY_LISTING))
            continue
        consec_empty = 0
        for u, t in new:
            seen.add(u)
        log(f"    page={page}: {len(new)} 篇（累计 {len(seen)}）")
        for u, t in new:
            yield u, t
        time.sleep(random.uniform(*DELAY_LISTING))


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    try:
        start_d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    except ValueError:
        print("起始日期格式错，应为 YYYY-MM-DD"); sys.exit(1)
    end_d = date.today()
    if len(sys.argv) >= 3:
        try:
            end_d = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        except ValueError:
            print("结束日期格式错"); sys.exit(1)
    if start_d > end_d:
        print("起始 > 结束"); sys.exit(1)

    print("=" * 64)
    print(f"RSS 公众号历史备查：{start_d} ~ {end_d}（14 个公众号 + VIP 跳转）")
    print("=" * 64)

    cookie = load_cookie()
    log(f"已读取 jintiankansha cookie（长度 {len(cookie)}）")

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

    state = daily_push.load_state(STATE_PATH)
    pushed_urls = set(state.get("pushed_urls", []))
    pushed_titles = set(state.get("pushed_titles", []))
    log(f"当前 state：{len(pushed_urls)} URL, {len(pushed_titles)} 标题")

    # ---- 阶段 1：扫 14 个专栏 listing ----
    log("\n阶段 1/2：扫描 14 个专栏的 listing")
    log("-" * 64)
    candidates = []
    for i, feed_url in enumerate(rss_lib.FEEDS, 1):
        try:
            col_link, col_title = rss_lib.get_channel_link(feed_url)
        except Exception as e:
            log(f"  ! [{i}/14] feed 元信息失败：{e}")
            continue
        if not col_link:
            continue
        log(f"  [{i}/14] {col_title}")
        for t_url, title in walk_column_listing(col_link, log):
            candidates.append((t_url, title, col_title))
    log(f"\n列表发现：{len(candidates)} 篇候选")

    # ---- 去重 ----
    seen_url = set(); after_url = []
    for tup in candidates:
        if tup[0] in seen_url: continue
        seen_url.add(tup[0]); after_url.append(tup)
    seen_t = set(); after_t = []; dup_t = 0
    for t_url, title, src in after_url:
        k = daily_push._norm_title(title)
        if k and k in seen_t:
            dup_t += 1; continue
        if k: seen_t.add(k)
        after_t.append((t_url, title, src))
    log(f"URL 去重 → {len(after_url)}；标题去重 → {len(after_t)}（跨专栏重 {dup_t}）")

    fresh = []; skipped = 0
    for t_url, title, src in after_t:
        if t_url in pushed_urls:
            skipped += 1; continue
        # 注：state 里的 pushed_urls 主要存的是 mp.weixin.qq.com URL（forward 流的）和 hexun URL。
        # jintiankansha 的 /t/URL 一般不在 state 里。所以这里更多靠标题去重。
        if daily_push._norm_title(title) in pushed_titles:
            skipped += 1; continue
        fresh.append((t_url, title, src))
    log(f"扣除 state 已推 {skipped} 条 → 待逐篇判断日期 {len(fresh)} 条")
    if not fresh:
        log("没有新候选。"); return

    est_min = len(fresh) * 4.0 / 60
    log(f"预计阶段 2 耗时 ~{est_min:.0f} 分钟（部分按日期会跳过，实际推送会更少）")
    ans = input("继续？(Y/n)：").strip().lower()
    if ans == "n": return

    # ---- 阶段 2：逐篇 解析 /t_original/ → WeChat → 推 ----
    log(f"\n阶段 2/2：跳转到 WeChat 拉正文 + 推送")
    log("-" * 64)
    n_ok = n_fail = n_oob = 0
    consec_resolve_fail = 0
    for i, (t_url, list_title, src) in enumerate(fresh, 1):
        t_id = rss_lib.t_id_from_url(t_url)
        # 1) jintian /t_original/ → WeChat URL
        try:
            wx_url = rss_lib.resolve_jintian_to_wechat(t_id, cookie)
        except Exception as e:
            consec_resolve_fail += 1
            log(f"[{i}/{len(fresh)}] ! 解析 WeChat URL 失败 [{t_id}]：{e}")
            n_fail += 1
            if consec_resolve_fail >= 5:
                log("!! 连续 5 次解析失败 — VIP cookie 可能已失效，停止")
                break
            time.sleep(random.uniform(*DELAY_ARTICLE))
            continue
        consec_resolve_fail = 0

        # 2) 抓 WeChat 文章页
        try:
            html = rss_lib.fetch_html(wx_url)
            a_title, a_dt, a_content = rss_lib.parse_wechat_article(html)
        except Exception as e:
            log(f"[{i}/{len(fresh)}] ! WeChat 抓取失败：{wx_url[:60]} → {e}")
            n_fail += 1
            time.sleep(random.uniform(*DELAY_ARTICLE))
            continue

        # 3) 日期过滤
        if not a_dt:
            log(f"[{i}/{len(fresh)}] ! 无法解析日期：{t_id}")
            n_fail += 1
            time.sleep(random.uniform(*DELAY_ARTICLE))
            continue
        if not (start_d <= a_dt.date() <= end_d):
            n_oob += 1
            if n_oob % 20 == 0:
                log(f"[{i}/{len(fresh)}] 跳过 {a_dt.date()}（范围外，累计 {n_oob}）")
            time.sleep(random.uniform(0.3, 0.7))
            continue
        if not a_content:
            log(f"[{i}/{len(fresh)}] ! 正文未找到：{wx_url[:60]}")
            n_fail += 1
            continue

        # 4) 二次去重（用 mp.weixin.qq.com URL，避免 forward 流刚推过）
        if wx_url in pushed_urls:
            n_oob += 1  # 复用计数，事实是已推过
            time.sleep(random.uniform(0.3, 0.7))
            continue

        title = a_title or list_title
        log(f"[{i}/{len(fresh)}] {a_dt.date()} [{src}] {title[:45]}")
        try:
            actual_title = daily_push.push_one(
                access_token, section_id,
                a_dt.date(), wx_url, title, log,
                prefetched_content=a_content, source_label=src,
            )
            pushed_urls.add(wx_url)
            pushed_urls.add(t_url)  # 把 jintiankansha URL 也记下，下次扫到能跳
            pushed_titles.add(daily_push._norm_title(title))
            if actual_title:
                pushed_titles.add(daily_push._norm_title(actual_title))
            n_ok += 1
        except Exception:
            err = traceback.format_exc().splitlines()[-1]
            log(f"  ! 推送失败：{err}")
            n_fail += 1

        if (n_ok + n_fail + n_oob) % STATE_SAVE_EVERY == 0:
            state["pushed_urls"] = sorted(pushed_urls)
            state["pushed_titles"] = sorted(pushed_titles)
            daily_push.save_state(STATE_PATH, state)
            log(f"  ✓ state 保存（推 {n_ok} / 跳过 {n_oob} / 失败 {n_fail}）")
        time.sleep(random.uniform(*DELAY_ARTICLE))

    state["pushed_urls"] = sorted(pushed_urls)
    state["pushed_titles"] = sorted(pushed_titles)
    daily_push.save_state(STATE_PATH, state)
    log("")
    log("=" * 64)
    log(f"完成。推送 {n_ok} 条，范围外/已存在跳过 {n_oob}，失败 {n_fail}")
    log("=" * 64)
    log("记得 commit state.json：")
    log(f"  cd {os.getcwd()}")
    log("  git add state.json && git -c commit.gpgsign=false commit -m 'rss backfill' && git push")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断（每 20 篇保存一次 state.json，再跑会接着推）")
