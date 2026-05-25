#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性补推：把指定日期范围内所有 5 个栏目的文章推送到 OneNote。

跑法（本地，不要在 GitHub Actions 跑）：

    cd ~/Desktop/hexun-onenote-rss
    python3 bulk_backfill.py 2026-01-01

可选第二个参数是结束日期（默认 = 今天）。

会从 ~/hexun-onenote-secrets.json 读 OAuth 凭据；如果该文件不存在则提示重跑 setup.py。
state.json 在本地直接读写——跑完后你手动 git add state.json && git commit && git push 同步到仓库。
"""

import json
import os
import random
import sys
import time
import traceback
from datetime import date, datetime, timedelta

import body_xhtml
import daily_push   # 复用 _norm_title / push_one / parse_article_dt
import hexun_lib
import onenote


SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")
STATE_PATH = "state.json"
DELAY_RANGE = (1.2, 2.5)

# 各栏目分页探测范围 (low, high)（None = 不翻页，只看 index.html）
# 范围尽量收紧以减少空请求；low 是历史下限，high 应 ≥ 已知最新数字页
SECTION_PAGES = {
    "bxhyzx": (660, 700),   # 已知 668 是 index.html 后第一个数字页
    "bxgsxw": (670, 700),   # 已知 679 是 index.html 后第一个数字页
    "bxjgdt": None,         # 实测无翻页（700-670 全空，只 index.html）
    "bxzjyy": None,
    "bxscpl": None,
}


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}")
        print("请先在本地跑 python3 setup.py 完成一次 OAuth 配置")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def section_url(section):
    suffix = "index.html" if section != "bxscpl" else ""
    return f"https://insurance.hexun.com/{section}/{suffix}"


def walk_section(section, start_d, end_d):
    """生成器：吐出 (date, url, title)；按列表页顺序，自动停在 < start_d 处。"""
    # 1. index.html
    url = section_url(section)
    try:
        raw = hexun_lib.fetch(url, referer="https://insurance.hexun.com/")
        entries = hexun_lib.parse_list_page(hexun_lib.decode_html(raw))
        log(f"  {section}/index.html → {len(entries)} 条")
    except Exception as e:
        log(f"  ! {section}/index.html 拉取失败：{e}")
        return
    for dt, art_url, title in entries:
        if start_d <= dt <= end_d:
            yield dt, art_url, title
    # 早停：若本页最新都比 start_d 早，没必要翻页
    if entries and max(e[0] for e in entries) < start_d:
        return
    time.sleep(random.uniform(*DELAY_RANGE))

    # 2. 数字翻页（如果该栏目支持）
    rng = SECTION_PAGES.get(section)
    if not rng:
        return
    low, high = rng
    consec_empty = 0
    found_any = False  # 只在已经找到过有效页之后才让 consec_empty 触发终止
    for n in range(high, low - 1, -1):
        try:
            raw = hexun_lib.fetch(
                f"https://insurance.hexun.com/{section}/index-{n}.html",
                referer="https://insurance.hexun.com/",
            )
            entries = hexun_lib.parse_list_page(hexun_lib.decode_html(raw))
        except Exception as e:
            log(f"  ! {section}/index-{n} 拉取失败：{e}")
            time.sleep(random.uniform(*DELAY_RANGE))
            continue
        if not entries:
            consec_empty += 1
            if found_any and consec_empty >= 5:
                log(f"  {section}/index-{n}: 连续空页，停止翻页")
                break
            time.sleep(random.uniform(0.5, 1.0))
            continue
        consec_empty = 0
        found_any = True
        dmin = min(e[0] for e in entries)
        dmax = max(e[0] for e in entries)
        log(f"  {section}/index-{n}: {len(entries)} 条 [{dmin}~{dmax}]")
        if dmax < start_d:
            log(f"  本页最新 {dmax} < {start_d}，停止翻页")
            break
        if dmin > end_d:
            time.sleep(random.uniform(*DELAY_RANGE))
            continue
        for dt, art_url, title in entries:
            if start_d <= dt <= end_d:
                yield dt, art_url, title
        time.sleep(random.uniform(*DELAY_RANGE))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    try:
        start_d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    except ValueError:
        print("起始日期格式错，应为 YYYY-MM-DD")
        sys.exit(1)
    end_d = date.today()
    if len(sys.argv) >= 3:
        try:
            end_d = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        except ValueError:
            print("结束日期格式错")
            sys.exit(1)
    if start_d > end_d:
        print("起始日期不能晚于结束日期")
        sys.exit(1)

    print("=" * 64)
    print(f"补推范围：{start_d} ~ {end_d}")
    print("=" * 64)

    secrets = load_secrets()
    client_id = secrets["AZURE_CLIENT_ID"]
    refresh_token = secrets["MS_REFRESH_TOKEN"]
    section_id = secrets["ONENOTE_SECTION_ID"]

    log("刷新 access_token...")
    access_token, new_refresh = onenote.refresh_access_token(client_id, refresh_token)
    if new_refresh != refresh_token:
        log("注意：refresh_token 已更新")
        secrets["MS_REFRESH_TOKEN"] = new_refresh
        with open(SECRETS_PATH, "w", encoding="utf-8") as f:
            json.dump(secrets, f, indent=2, ensure_ascii=False)
        log(f"已写回 {SECRETS_PATH}（下次记得同步到 GitHub Secret）")

    state = daily_push.load_state(STATE_PATH)
    pushed_urls = set(state.get("pushed_urls", []))
    pushed_titles = set(state.get("pushed_titles", []))
    log(f"当前 state：{len(pushed_urls)} URL, {len(pushed_titles)} 标题")

    # 1. 走 5 个栏目，收集所有候选
    log("\n阶段 1/2：搜集候选 URL")
    log("-" * 64)
    all_candidates = []
    for section in ["bxhyzx", "bxjgdt", "bxgsxw", "bxzjyy", "bxscpl"]:
        log(f"扫描 {section}...")
        for tup in walk_section(section, start_d, end_d):
            all_candidates.append(tup)
    log(f"\n汇总 {len(all_candidates)} 条")

    # 去 batch 内 URL 重
    seen_url = set()
    after_url = []
    for tup in all_candidates:
        if tup[1] in seen_url:
            continue
        seen_url.add(tup[1])
        after_url.append(tup)
    # 去 batch 内标题重（保留最早出现的）
    seen_t = set()
    after_t = []
    title_dups = 0
    for dt, url, title in after_url:
        k = daily_push._norm_title(title)
        if k and k in seen_t:
            title_dups += 1
            continue
        if k:
            seen_t.add(k)
        after_t.append((dt, url, title))
    log(f"URL 去重 → {len(after_url)}；标题去重 → {len(after_t)}（跨栏目重 {title_dups}）")

    # 排除 state 中已有
    to_push = []
    skipped_state = 0
    for dt, url, title in after_t:
        if url in pushed_urls:
            skipped_state += 1
            continue
        if daily_push._norm_title(title) in pushed_titles:
            skipped_state += 1
            continue
        to_push.append((dt, url, title))
    log(f"剔除 state 已推 {skipped_state} 条 → 实际待推 {len(to_push)} 条")

    if not to_push:
        log("没有新文章要推送。")
        return

    # 按时间升序推（旧的先，新的后；OneNote 排序更友好）
    to_push.sort(key=lambda x: x[0])
    avg_sec = 2 + sum(DELAY_RANGE) / 2  # 网络下载 + 上传 + 延时
    log(f"预计耗时 ~{len(to_push) * avg_sec / 60:.1f} 分钟")
    ans = input("继续？(Y/n)：").strip().lower()
    if ans == "n":
        return

    # 2. 推
    log(f"\n阶段 2/2：推送到 OneNote")
    log("-" * 64)
    n_ok = n_fail = 0
    for i, (dt, url, title) in enumerate(to_push, 1):
        log(f"[{i}/{len(to_push)}] {dt}  {title[:50]}")
        try:
            actual_title = daily_push.push_one(access_token, section_id, dt, url, title, log)
            pushed_urls.add(url)
            pushed_titles.add(daily_push._norm_title(title))
            if actual_title:
                pushed_titles.add(daily_push._norm_title(actual_title))
            n_ok += 1
        except Exception:
            err = traceback.format_exc()
            log(f"  ! 失败：{err.splitlines()[-1]}")
            n_fail += 1
        # 每 20 篇刷一次 state，防中断丢进度
        if i % 20 == 0:
            state["pushed_urls"] = sorted(pushed_urls)
            state["pushed_titles"] = sorted(pushed_titles)
            daily_push.save_state(STATE_PATH, state)
            log(f"  ✓ state 已保存（{n_ok}/{i}）")
        time.sleep(random.uniform(*DELAY_RANGE))

    state["pushed_urls"] = sorted(pushed_urls)
    state["pushed_titles"] = sorted(pushed_titles)
    daily_push.save_state(STATE_PATH, state)
    log(f"\n结束。成功 {n_ok}，失败 {n_fail}")
    log(f"state.json 已更新，记得：")
    log(f"  cd {os.getcwd()}")
    log(f"  git add state.json && git commit -m 'bulk backfill' && git push")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断（已推送的不会丢失，state.json 每 20 篇保存一次）")
