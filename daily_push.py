#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 调用的主程序：

1. 拉和讯 bxhyzx 列表页
2. 过滤掉 state.json 中已推送的 URL
3. 若 state 为空（首次运行），只推送过去 48 小时内的文章作为冷启动
4. 对每篇新文章：拉正文 → 转 OneNote XHTML → 下载图片 → 发布
5. 推送成功后把 URL 追加到 state.json，并保留最近 1000 条
6. 任何不可恢复错误都会以一个【ERROR】页推送到同一分区

环境变量：
  AZURE_CLIENT_ID         必填
  MS_REFRESH_TOKEN        必填
  ONENOTE_SECTION_ID      必填
  STATE_FILE              选填，默认 ./state.json
"""

import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import body_xhtml
import hexun_lib
import onenote


BEIJING = timezone(timedelta(hours=8))

STATE_KEEP = 1000          # 最近 N 条 URL 留作去重
BOOTSTRAP_HOURS = 48        # 首跑只追溯过去 N 小时
DELAY_RANGE = (1.5, 3.5)


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"环境变量 {name} 未设置")
    return v


def load_state(path):
    if not os.path.exists(path):
        return {"pushed_urls": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "pushed_urls" not in data:
            data["pushed_urls"] = []
        return data
    except Exception:
        return {"pushed_urls": []}


def save_state(path, state):
    state["pushed_urls"] = state["pushed_urls"][-STATE_KEEP:]
    state["updated_at"] = datetime.now(BEIJING).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_article_dt(publish_str, fallback_date):
    """publish_str 是 'YYYY-MM-DD HH:MM:SS' 北京时间。"""
    if publish_str:
        try:
            dt = datetime.strptime(publish_str, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=BEIJING)
        except ValueError:
            pass
    return datetime(fallback_date.year, fallback_date.month, fallback_date.day,
                    12, 0, 0, tzinfo=BEIJING)


def build_page_title(art_dt, title):
    """260525标题... 格式（2 位年 + 月 + 日 + 标题，无分隔符）"""
    return f"{art_dt.strftime('%y%m%d')}{title}"


def collect_new_articles(state, log):
    """从 index.html 拉最新一页，找出 state 里没记录的文章。"""
    raw = hexun_lib.fetch(hexun_lib.LIST_URL, referer="https://insurance.hexun.com/")
    html = hexun_lib.decode_html(raw)
    entries = hexun_lib.parse_list_page(html)
    log(f"列表页 {len(entries)} 条")
    pushed = set(state.get("pushed_urls", []))
    is_first_run = len(pushed) == 0
    if is_first_run:
        cutoff = datetime.now(BEIJING) - timedelta(hours=BOOTSTRAP_HOURS)
        log(f"首次运行，仅推送 {cutoff.date()} 之后（≈过去 {BOOTSTRAP_HOURS} 小时）的文章；其他记入 state")
    new_items = []
    skipped_old = 0
    for dt, url, list_title in entries:
        if url in pushed:
            continue
        if is_first_run:
            # 用日期粗筛；时分秒在文章页拉到后再精筛也行，这里先按日期
            if datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=BEIJING) < cutoff:
                pushed.add(url)
                skipped_old += 1
                continue
        new_items.append((dt, url, list_title))
    state["pushed_urls"] = sorted(pushed)
    if is_first_run:
        log(f"  跳过 {skipped_old} 条更老的文章（已记入 state）")
    log(f"待推送 {len(new_items)} 条")
    return new_items


# 图片格式 magic bytes
IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff":      "image/jpeg",
    b"GIF87a":            "image/gif",
    b"GIF89a":            "image/gif",
    b"RIFF":              "image/webp",   # 后 4 字节是大小，再后面 "WEBP"
}


def _detect_image(bts):
    """返回 (是否为有效图片, mime_type)"""
    if not bts or len(bts) < 32:
        return False, None
    for magic, mime in IMAGE_MAGIC.items():
        if bts.startswith(magic):
            if mime == "image/webp" and bts[8:12] != b"WEBP":
                continue
            return True, mime
    return False, None


def push_one(access_token, section_id, dt, url, list_title, log):
    log(f"→ 拉取 {url}")
    raw = hexun_lib.fetch(url, referer=hexun_lib.LIST_URL)
    html = hexun_lib.decode_html(raw)
    title, publish_str, source = hexun_lib.extract_article_meta(html)
    body_html_raw = hexun_lib.extract_body_html(html)
    if not body_html_raw:
        raise RuntimeError("正文区 art_contextBox 未找到")
    xhtml, image_urls = body_xhtml.convert(body_html_raw, base_url=url)
    art_dt = parse_article_dt(publish_str, dt)
    final_title = build_page_title(art_dt, title or list_title)

    # 下载所有图片，每张做 magic-byte 校验；失败的从 XHTML 里剔除并留文字标记
    image_blobs = []         # 仅保留有效图，按原顺序
    valid_indices = []       # 原图序号 → 在 image_blobs 中的新序号
    for i, img_url in enumerate(image_urls):
        try:
            bts, ctype = hexun_lib.fetch_binary(img_url, referer=url)
        except Exception as e:
            log(f"  ! 图片下载失败 [{i}]：{img_url} → {e}")
            bts, ctype = b"", None
        is_img, sniffed = _detect_image(bts)
        if not is_img:
            log(f"  ! 图片校验失败 [{i}] ({len(bts)}B ctype={ctype})：{img_url}")
            continue
        new_idx = len(image_blobs)
        image_blobs.append((bts, sniffed or ctype))
        valid_indices.append((i, new_idx))

    # 重写 XHTML：失败的 img 替换成文字标记，幸存的 img 重新编号
    failed_set = {i for i in range(len(image_urls))} - {old for old, _ in valid_indices}
    for old_i in sorted(failed_set):
        xhtml = xhtml.replace(f'<img src="name:img{old_i}" />', '<p>[图片未能获取]</p>')
    # 把幸存图重新编号到 0..N-1
    # 用临时占位防止重号覆盖
    for old_i, new_i in valid_indices:
        xhtml = xhtml.replace(f'<img src="name:img{old_i}" />', f'<img src="name:_TMP{new_i}_" />')
    for _, new_i in valid_indices:
        xhtml = xhtml.replace(f'<img src="name:_TMP{new_i}_" />', f'<img src="name:img{new_i}" />')

    # 顶部 meta：原文链接保留可点击，正文里的链接由 body_xhtml 剥掉
    meta_header = (
        f'<p><b>来源：</b>{onenote._x_escape(source or "")} '
        f'<b>发布时间：</b>{onenote._x_escape(publish_str or "")} '
        f'<br/><b>原文：</b><a href="{onenote._x_escape(url)}">{onenote._x_escape(url)}</a></p>'
        f'<hr />'
    )
    full_body = meta_header + xhtml

    onenote.create_page(
        access_token, section_id,
        final_title, full_body, image_blobs,
        created_iso=art_dt.isoformat(),
    )
    log(f"  ✓ 已推送：{final_title[:60]}")


def push_error_page(access_token, section_id, err_text):
    try:
        now = datetime.now(BEIJING)
        title = f"{now.strftime('%Y%m%d')}【ERROR】hexun-onenote-rss 运行失败"
        body = (
            f"<p><b>时间：</b>{now.isoformat()}</p>"
            f"<p><b>错误详情：</b></p>"
            f"<pre>{onenote._x_escape(err_text)}</pre>"
        )
        onenote.create_page(access_token, section_id, title, body, [], created_iso=now.isoformat())
    except Exception as e:
        print(f"!! 连错误页都推不上去：{e}", file=sys.stderr)


def main():
    client_id = _env("AZURE_CLIENT_ID")
    refresh_token = _env("MS_REFRESH_TOKEN")
    section_id = _env("ONENOTE_SECTION_ID")
    state_path = os.environ.get("STATE_FILE", "state.json")

    def log(msg):
        ts = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    log("启动 hexun-onenote-rss")
    log(f"section_id={section_id[:12]}…  state_file={state_path}")

    # 1. 刷新 access_token（顺便看是否还有效）
    try:
        access_token, new_refresh = onenote.refresh_access_token(client_id, refresh_token)
    except Exception as e:
        # 刷新都失败，没法推错误页 → 直接退出非零
        log(f"!! 刷新 token 失败：{e}")
        raise
    if new_refresh != refresh_token:
        log("注意：refresh_token 已更新，请到 GitHub Secrets 更新 MS_REFRESH_TOKEN。新值见下一行：")
        log(new_refresh)

    state = load_state(state_path)

    try:
        items = collect_new_articles(state, log)
    except Exception:
        err = traceback.format_exc()
        log(f"!! 列表抓取失败：\n{err}")
        push_error_page(access_token, section_id, err)
        save_state(state_path, state)
        raise

    n_ok, n_fail = 0, 0
    pushed = set(state.get("pushed_urls", []))
    for i, (dt, url, title) in enumerate(items, 1):
        log(f"[{i}/{len(items)}] {dt} {title[:40]}")
        try:
            push_one(access_token, section_id, dt, url, title, log)
            pushed.add(url)
            n_ok += 1
        except Exception:
            err = traceback.format_exc()
            log(f"  ! 失败：\n{err}")
            push_error_page(access_token, section_id, f"文章: {url}\n标题: {title}\n\n{err}")
            n_fail += 1
        time.sleep(random.uniform(*DELAY_RANGE))

    state["pushed_urls"] = sorted(pushed)
    save_state(state_path, state)
    log(f"结束。成功 {n_ok}，失败 {n_fail}")
    if n_fail and not n_ok:
        sys.exit(2)  # 全失败：让 Actions 标红


if __name__ == "__main__":
    main()
