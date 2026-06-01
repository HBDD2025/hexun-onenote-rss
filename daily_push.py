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
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import body_xhtml
import hexun_lib
import onenote
import rss_lib


PER_ARTICLE_TIMEOUT_SEC = 120   # 单篇 wallclock 硬上限（信号兜底）
IMG_BUDGET_SEC = 90             # 单篇所有图加起来的下载预算，超了剩下全占位

# 占位文字（三种语义）
PLACEHOLDER_FAILED = '<p>【此处有图片，但未下载成功】</p>'    # 图片下载/校验失败
PLACEHOLDER_FIRST_STRIPPED = '<p>【本处已删首图】</p>'         # 源特定：首图被强制删
PLACEHOLDER_STRIPPED = '<p>【此处有删掉的图片】</p>'             # 源特定：非首图主动删（如大图广告）


class _ArticleTimeout(Exception):
    pass


def _article_timeout_handler(signum, frame):
    raise _ArticleTimeout(f"push_one 超过 {PER_ARTICLE_TIMEOUT_SEC} 秒，强制终止")


BEIJING = timezone(timedelta(hours=8))

STATE_KEEP = 2000          # 最近 N 条 URL/标题 留作去重
BOOTSTRAP_HOURS = 48        # 首跑只追溯过去 N 小时
MAX_AGE_DAYS = 7            # 任何源里超过这个天数的文章一律忽略（防止 bxzjyy/bxscpl 等单页含远古内容的栏目漏推）
DELAY_RANGE = (1.5, 3.5)
NEW_REFRESH_TOKEN_FILE = "_new_refresh_token.tmp"  # 检测到 token 轮换时写到这；workflow 会捕获


def _norm_title(t):
    """标题归一化：去全部空白，便于跨栏目去重。"""
    if not t:
        return ""
    return re.sub(r"\s+", "", t)


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"环境变量 {name} 未设置")
    return v


def load_state(path):
    base = {"pushed_urls": [], "pushed_titles": []}
    if not os.path.exists(path):
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return base


def save_state(path, state):
    state["pushed_urls"] = state["pushed_urls"][-STATE_KEEP:]
    state["pushed_titles"] = state["pushed_titles"][-STATE_KEEP:]
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
    """
    汇集和讯 5 栏目 + 14 个 RSS 源的候选；URL + 归一化标题双重去重。
    返回 [(dt, url, title, content_html_or_None, source_label), ...]
    content_html=None 表示需要在推送时拉文章页（仅和讯）；
    content_html=str 表示 RSS 里已带正文。
    """
    pushed_urls = set(state.get("pushed_urls", []))
    pushed_titles = set(state.get("pushed_titles", []))
    is_first_run = len(pushed_urls) == 0
    cutoff = datetime.now(BEIJING) - timedelta(hours=BOOTSTRAP_HOURS)
    age_cutoff = datetime.now(BEIJING) - timedelta(days=MAX_AGE_DAYS)
    if is_first_run:
        log(f"首次运行，仅推送 {cutoff.date()} 之后（≈过去 {BOOTSTRAP_HOURS} 小时）的文章；其他记入 state")
    else:
        log(f"忽略 {age_cutoff.date()} 之前的文章（>{MAX_AGE_DAYS} 天，防远古内容意外补入）")

    # --- 1a. 抓和讯 5 个栏目 ---
    all_entries = []  # 统一形状: (dt, url, title, content_or_none, source_label)
    hexun_total = 0
    for list_url in hexun_lib.LIST_URLS:
        try:
            raw = hexun_lib.fetch(list_url, referer="https://insurance.hexun.com/")
            entries = hexun_lib.parse_list_page(hexun_lib.decode_html(raw))
            log(f"和讯 {list_url} → {len(entries)} 条")
            if len(entries) == 0:
                # 可能是 WAF 给了非挑战页但内容空 / 页面结构变了，提示一下排查
                log(f"    （0 条警告：拉到 {len(raw)}B，可能页面结构变化或反爬过滤）")
            hexun_total += len(entries)
            for dt, url, title in entries:
                all_entries.append((dt, url, title, None, "和讯"))
        except Exception as e:
            log(f"  ! 列表抓取失败：{list_url} → {e}")
    if hexun_total == 0:
        log("!! 和讯 5 个栏目全部 0 条 —— 极可能 GitHub Actions IP 被腾讯 EdgeOne 拦截，查上面 stderr 的 CHAOS_VM 提示")

    # --- 1b. 抓 14 个 RSS 源 ---
    for feed_url in rss_lib.FEEDS:
        try:
            chan_title, items = rss_lib.parse_feed(feed_url)
            log(f"RSS {chan_title or feed_url[-30:]} → {len(items)} 条")
            for it in items:
                if not it.get("link") or not it.get("title"):
                    continue
                if it.get("date") is None:
                    continue
                all_entries.append((
                    it["date"], it["link"], it["title"],
                    it["content_html"], chan_title or "RSS",
                ))
        except Exception as e:
            log(f"  ! RSS 拉取失败：{feed_url[-30:]} → {e}")

    # --- 2. batch 内按 URL 去重 ---
    seen_urls_batch = set()
    after_url_dedup = []
    for tup in all_entries:
        url = tup[1]
        if url in seen_urls_batch:
            continue
        seen_urls_batch.add(url)
        after_url_dedup.append(tup)

    # --- 3. batch 内按归一化标题去重 ---
    seen_titles_batch = set()
    deduped = []
    title_collisions = 0
    for tup in after_url_dedup:
        title = tup[2]
        key = _norm_title(title)
        if key and key in seen_titles_batch:
            title_collisions += 1
            continue
        if key:
            seen_titles_batch.add(key)
        deduped.append(tup)
    log(f"汇总 {len(all_entries)} → URL 去重 {len(after_url_dedup)} → 标题去重 {len(deduped)}（跨源重复 {title_collisions} 条）")

    # --- 4. 对比 state，排除已推过的 + 超过 MAX_AGE_DAYS 的 ---
    new_items = []
    skipped_old = 0
    for dt, url, title, content, source in deduped:
        if url in pushed_urls:
            continue
        if _norm_title(title) in pushed_titles:
            continue
        # 通用年龄过滤（包括首跑和非首跑）
        article_end_of_day = datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=BEIJING)
        if article_end_of_day < age_cutoff:
            pushed_urls.add(url)
            pushed_titles.add(_norm_title(title))
            skipped_old += 1
            continue
        # 首跑额外的更严格 48h 窗口
        if is_first_run and article_end_of_day < cutoff:
            pushed_urls.add(url)
            pushed_titles.add(_norm_title(title))
            skipped_old += 1
            continue
        new_items.append((dt, url, title, content, source))

    state["pushed_urls"] = sorted(pushed_urls)
    state["pushed_titles"] = sorted(pushed_titles)
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


# 小于这个 min(width, height) 的图视作图标/装饰，整段抹掉（不放占位）
TINY_IMAGE_MIN_SIDE = 80


def _get_image_dims(bts):
    """从已下载图片字节里读 (width, height)。失败返回 (None, None)。
    支持 PNG / JPEG / GIF / WebP（VP8/VP8L/VP8X），全部 stdlib 实现。"""
    if not bts or len(bts) < 24:
        return None, None
    try:
        # PNG: IHDR 在偏移 12，宽高紧随其后（big-endian 4B）
        if bts[:8] == b"\x89PNG\r\n\x1a\n" and bts[12:16] == b"IHDR":
            return (int.from_bytes(bts[16:20], "big"),
                    int.from_bytes(bts[20:24], "big"))
        # GIF: 偏移 6 处 width/height 小端 2B
        if bts[:6] in (b"GIF87a", b"GIF89a"):
            return (int.from_bytes(bts[6:8], "little"),
                    int.from_bytes(bts[8:10], "little"))
        # JPEG: 扫 SOF 段
        if bts[:3] == b"\xff\xd8\xff":
            i = 2
            n = len(bts)
            while i + 9 < n:
                if bts[i] != 0xFF:
                    break
                # 跳过填充 0xFF
                while i < n and bts[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = bts[i]
                i += 1
                # 无 length 的标记
                if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
                    continue
                if i + 1 >= n:
                    break
                seg_len = int.from_bytes(bts[i:i+2], "big")
                # SOFn（除 0xC4 DHT、0xC8 JPG、0xCC DAC）
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    if i + 7 < n:
                        h = int.from_bytes(bts[i+3:i+5], "big")
                        w = int.from_bytes(bts[i+5:i+7], "big")
                        return w, h
                i += seg_len
        # WebP
        if bts[:4] == b"RIFF" and bts[8:12] == b"WEBP":
            fourcc = bts[12:16]
            if fourcc == b"VP8X" and len(bts) >= 30:
                w = (int.from_bytes(bts[24:27], "little") & 0xFFFFFF) + 1
                h = (int.from_bytes(bts[27:30], "little") & 0xFFFFFF) + 1
                return w, h
            if fourcc == b"VP8 " and len(bts) >= 30:
                w = int.from_bytes(bts[26:28], "little") & 0x3FFF
                h = int.from_bytes(bts[28:30], "little") & 0x3FFF
                return w, h
            if fourcc == b"VP8L" and len(bts) >= 25 and bts[20] == 0x2F:
                b1, b2, b3, b4 = bts[21], bts[22], bts[23], bts[24]
                w = (((b2 & 0x3F) << 8) | b1) + 1
                h = (((b4 & 0x0F) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6)) + 1
                return w, h
    except Exception:
        pass
    return None, None


# ---------- 源特定规则 ----------

# 每篇文章首图永远是 banner/logo，强制删除
STRIP_FIRST_IMG_SOURCES = ("慧保天下", "中国银行保险报", "今日保", "保契", "13个精算师")
# 慧保天下：除了删首图，还要删 [首图前的所有内容] + [首图后第一段]
STRIP_AGGRESSIVE_SOURCES = ("慧保天下",)
# 全部图删除
STRIP_ALL_IMG_SOURCES = ("中国保险学会",)
# 最后一张图永远是装饰（中国银行保险报常在文末贴一张落款/二维码图），静默删除（无占位）
STRIP_LAST_IMG_SOURCES = ("中国银行保险报",)
# 按文本锚点处理。每条 (source_kw, anchor_text, action)
# action:
#   "all_after"     → anchor 所在段落及之后全删（文字+图）
#   "next_img"      → anchor 之后第一张 <img/> 删除（替换占位）
#   "prev_img"      → anchor 之前最后一张 <img/> 删除（替换占位）
#   "imgs_after_strip" → anchor 之后所有 <img/> 删除（全部替换占位）
SOURCE_TEXT_RULES = (
    ("中国银行保险报", "来源:", "all_after"),   # 先匹配半角冒号
    ("中国银行保险报", "来源：", "all_after"),
    ("保观",          "保观 | 聚焦保险创新", "next_img"),
    ("保险一哥",       "请加微信", "imgs_after_strip"),
    ("保险一哥",       "文章原文", "prev_img"),
)


# 慧保天下的促销 caption 关键字（必须组合出现，单"长按"不算）
_HUIBAO_PROMO_KEYWORDS = re.compile(
    r'长按.{0,15}(?:二维码|识别|关注|加群|入群|报名|添加|订阅|图片|图中|下方|以下)|'
    r'扫描.{0,15}(?:二维码|图片|关注|添加|下方|以下)|'
    r'扫码.{0,15}(?:关注|添加|报名|订阅|入群|加群|查看|获取)|'
    r'扫一扫.{0,15}(?:关注|二维码|添加)?|'
    r'^\s*[▲▼◆●※☆★].{0,30}$|'   # 三角/符号开头的整段（caption 特征）
    r'添加微信|加入.{0,5}群|关注我们|订阅号|公众号.{0,5}(?:关注|订阅)'
)


def _find_huibao_promo_paragraphs(xhtml):
    """yield 短促销段 <p>...</p> 的 match。判定：纯文本 ≤40 字 + 命中关键字组合。"""
    for m in re.finditer(r'<p[^>]*>(.*?)</p>', xhtml, re.S):
        inner_text = re.sub(r'<[^>]+>', '', m.group(1))
        inner_text = inner_text.replace('&nbsp;', '').replace('\xa0', '')
        inner_text = re.sub(r'\s+', '', inner_text)
        if len(inner_text) == 0 or len(inner_text) > 40:
            continue
        if _HUIBAO_PROMO_KEYWORDS.search(inner_text):
            yield m


def _drop_img_in_xhtml(xhtml, action_match_re):
    """删 xhtml 里第一个 <img/> 标签，标签由 action_match_re 指定（应捕获 name:imgN 引用）。
    返回 (new_xhtml, did_strip)"""
    m = action_match_re.search(xhtml)
    if not m:
        return xhtml, False
    return xhtml[:m.start()] + xhtml[m.end():], True


_IMG_TAG_RE = re.compile(r'<img\s+src="name:img\d+"\s*/>')


def _apply_source_rules(xhtml, image_urls, source_label):
    """统一应用所有源特定剥图规则。最后统一重编号。"""
    if not source_label or not image_urls:
        return xhtml, image_urls

    # 1. 中国保险学会：全图替换占位
    if any(s in source_label for s in STRIP_ALL_IMG_SOURCES):
        xhtml = _IMG_TAG_RE.sub(PLACEHOLDER_FAILED, xhtml)
        return xhtml, []

    # 2. 首图删除（替换为占位「本处已删首图」）
    aggressive = any(s in source_label for s in STRIP_AGGRESSIVE_SOURCES)
    simple_first = any(s in source_label for s in STRIP_FIRST_IMG_SOURCES)
    if aggressive:
        # 慧保天下 第一步：删 [开头到首图（含）] + [首图后第一段]，首图位置插占位
        m = re.search(r'<img\s+src="name:img0"\s*/>', xhtml)
        if m:
            after = xhtml[m.end():]
            after = re.sub(
                r'^\s*(?:<br\s*/?>\s*)*<p[^>]*>.*?</p>\s*',
                '', after, count=1, flags=re.S,
            )
            xhtml = PLACEHOLDER_FIRST_STRIPPED + after

        # 慧保天下 第二步：找促销 caption（长按/扫码/▲/扫描二维码 等），删该段 + 段前最近一张图
        # （处理大图广告 + caption 模式，可循环多次）
        for _ in range(8):  # 最多 8 次防御性死循环
            anchors = list(_find_huibao_promo_paragraphs(xhtml))
            if not anchors:
                break
            am = anchors[0]  # 先处理第一个
            last_img = None
            for im in _IMG_TAG_RE.finditer(xhtml[:am.start()]):
                last_img = im
            if last_img:
                xhtml = (
                    xhtml[:last_img.start()]
                    + PLACEHOLDER_STRIPPED
                    + xhtml[last_img.end():am.start()]
                    + xhtml[am.end():]
                )
            else:
                xhtml = xhtml[:am.start()] + xhtml[am.end():]
    elif simple_first:
        # 把首图（含包裹 <p>）替换为占位段
        new_xhtml, n = re.subn(
            r'<p[^>]*>\s*<img\s+src="name:img0"\s*/>\s*</p>',
            PLACEHOLDER_FIRST_STRIPPED, xhtml, count=1,
        )
        if n == 0:
            new_xhtml = re.sub(
                r'<img\s+src="name:img0"\s*/>',
                PLACEHOLDER_FIRST_STRIPPED, xhtml, count=1,
            )
        xhtml = new_xhtml

    # 2b. 末图删除（中国银行保险报常在文末贴装饰图，无文本锚点时漏网）
    if any(s in source_label for s in STRIP_LAST_IMG_SOURCES):
        # 优先连同包裹 <p> 一起删；否则裸 <img/> 删
        last_p = None
        for m in re.finditer(
            r'<p[^>]*>\s*<img\s+src="name:img\d+"\s*/>\s*</p>',
            xhtml,
        ):
            last_p = m
        if last_p:
            xhtml = xhtml[:last_p.start()] + xhtml[last_p.end():]
        else:
            last_bare = None
            for m in _IMG_TAG_RE.finditer(xhtml):
                last_bare = m
            if last_bare:
                xhtml = xhtml[:last_bare.start()] + xhtml[last_bare.end():]

    # 3. 文本锚点规则
    for src_kw, anchor, action in SOURCE_TEXT_RULES:
        if src_kw not in source_label:
            continue
        idx = xhtml.find(anchor)
        if idx < 0:
            continue
        if action == "all_after":
            # 砍掉锚点所在 <p> 的开头到末尾全部
            p_start = xhtml.rfind('<p', 0, idx)
            xhtml = xhtml[:p_start] if p_start >= 0 else xhtml[:idx]
            break  # 砍完直接退，后续锚点无意义
        elif action == "next_img":
            head = xhtml[:idx + len(anchor)]
            tail = xhtml[idx + len(anchor):]
            tail = _IMG_TAG_RE.sub(PLACEHOLDER_FAILED, tail, count=1)
            xhtml = head + tail
        elif action == "prev_img":
            before = xhtml[:idx]
            after = xhtml[idx:]
            last_img = None
            for m in _IMG_TAG_RE.finditer(before):
                last_img = m
            if last_img:
                xhtml = (before[:last_img.start()]
                         + PLACEHOLDER_FAILED
                         + before[last_img.end():] + after)
        elif action == "imgs_after_strip":
            head = xhtml[:idx + len(anchor)]
            tail = xhtml[idx + len(anchor):]
            tail = _IMG_TAG_RE.sub(PLACEHOLDER_STRIPPED, tail)
            xhtml = head + tail

    # 4. 最终重编号：根据 xhtml 里剩下的 name:imgN 引用，连续编号 0..K-1，过滤 image_urls
    used = sorted(set(int(m.group(1)) for m in re.finditer(r'name:img(\d+)', xhtml)))
    if not used:
        return xhtml, []
    if used == list(range(len(used))) and len(used) == len(image_urls):
        return xhtml, image_urls  # 没改变，原样
    # 用临时占位防覆盖
    for old in used:
        xhtml = xhtml.replace(f'name:img{old}', f'name:_FIN{old}_')
    for new, old in enumerate(used):
        xhtml = xhtml.replace(f'name:_FIN{old}_', f'name:img{new}')
    new_urls = [image_urls[old] for old in used if old < len(image_urls)]
    return xhtml, new_urls


# 兼容老函数名（保持外部接口稳定）
_maybe_strip_first_image = _apply_source_rules


def push_one(access_token, section_id, dt, url, list_title, log,
             prefetched_content=None, source_label=None):
    """
    prefetched_content：RSS 源已带正文 HTML 时传入；为 None 则按 hexun 流程拉文章页。
    source_label：用于顶部 meta 显示（RSS 用频道名，hexun 用原文里 <a> 的来源）。
    """
    if prefetched_content is not None:
        # RSS 路径：正文已有，无需再请求
        title = list_title
        publish_str = dt.strftime("%Y-%m-%d")
        source = source_label or ""
        body_html_raw = prefetched_content
    else:
        log(f"→ 拉取 {url}")
        raw = hexun_lib.fetch(url, referer=hexun_lib.LIST_URL)
        html = hexun_lib.decode_html(raw)
        title, publish_str, source = hexun_lib.extract_article_meta(html)
        body_html_raw = hexun_lib.extract_body_html(html)
        if not body_html_raw:
            raise RuntimeError("正文区 art_contextBox 未找到")
    xhtml, image_urls = body_xhtml.convert(body_html_raw, base_url=url)
    # 特定公众号首图（banner）强制剥掉
    xhtml, image_urls = _maybe_strip_first_image(xhtml, image_urls, source_label)
    art_dt = parse_article_dt(publish_str, dt)
    final_title = build_page_title(art_dt, title or list_title)

    # 顺序下载图片，但单篇总图下载时间硬性限制 IMG_BUDGET_SEC
    image_blobs = []         # 仅保留有效图，按原顺序
    valid_indices = []       # 原图序号 → 在 image_blobs 中的新序号
    tiny_indices = set()     # 被识别为小图标的原图序号，从正文里整段抹掉（无占位）
    budget_deadline = time.time() + IMG_BUDGET_SEC
    budget_exhausted = False
    for i, img_url in enumerate(image_urls):
        if budget_exhausted or time.time() >= budget_deadline:
            if not budget_exhausted:
                log(f"  ! 图片下载预算 {IMG_BUDGET_SEC}s 用完，剩 {len(image_urls)-i} 张全部置为占位")
                budget_exhausted = True
            continue
        try:
            bts, ctype = hexun_lib.fetch_binary(img_url, referer=url)
        except _ArticleTimeout:
            raise
        except Exception as e:
            log(f"  ! 图片下载失败 [{i}]：{img_url[:80]} → {e}")
            bts, ctype = b"", None
        is_img, sniffed = _detect_image(bts)
        if not is_img:
            log(f"  ! 图片校验失败 [{i}] ({len(bts)}B ctype={ctype})：{img_url[:80]}")
            continue
        # 小图标检测：min(w,h) < TINY_IMAGE_MIN_SIDE 视作装饰图，直接从正文抹掉
        # （微信图文常把 16×16~50×50 的 emoji/箭头/角标塞进 <img>，OneNote 会把
        # 这些小图按 outline 宽度放大成大图，必须识别去除）
        w, h = _get_image_dims(bts)
        if w and h and min(w, h) < TINY_IMAGE_MIN_SIDE:
            log(f"  · 忽略小图标 [{i}] {w}×{h}：{img_url[:60]}")
            tiny_indices.add(i)
            continue
        new_idx = len(image_blobs)
        image_blobs.append((bts, sniffed or ctype))
        valid_indices.append((i, new_idx))

    # 抹掉小图标的 <img> 引用（连同独占的 <p> 包裹）
    for old_i in sorted(tiny_indices):
        # 先试整段 <p><img/></p>
        xhtml = re.sub(
            r'<p[^>]*>\s*<img\s+src="name:img' + str(old_i) + r'"\s*/>\s*</p>',
            '', xhtml,
        )
        # 兜底：裸 <img/>（可能和文字混排）
        xhtml = xhtml.replace(f'<img src="name:img{old_i}" />', '')

    # 重写 XHTML：失败的 img 替换成文字标记，幸存的 img 重新编号
    failed_set = (
        {i for i in range(len(image_urls))}
        - {old for old, _ in valid_indices}
        - tiny_indices
    )
    for old_i in sorted(failed_set):
        xhtml = xhtml.replace(f'<img src="name:img{old_i}" />', PLACEHOLDER_FAILED)
    # 把幸存图重新编号到 0..N-1
    # 用临时占位防止重号覆盖
    for old_i, new_i in valid_indices:
        xhtml = xhtml.replace(f'<img src="name:img{old_i}" />', f'<img src="name:_TMP{new_i}_" />')
    for _, new_i in valid_indices:
        xhtml = xhtml.replace(f'<img src="name:_TMP{new_i}_" />', f'<img src="name:img{new_i}" />')

    # 顶部 meta：第一行 来源 + 原文链接；第二行 发布时间 + 推送时间
    # 前面留 3 个空段落，避免长标题与正文重叠
    push_time = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    _event = os.environ.get("GITHUB_EVENT_NAME", "")
    trigger_label = {
        "schedule": "定时",
        "workflow_dispatch": "手动",
    }.get(_event, "本地")
    meta_header = (
        '<p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>'
        f'<p><b>来源：</b>{onenote._x_escape(source or "")}'
        f' &nbsp;|&nbsp; '
        f'<a href="{onenote._x_escape(url)}">原文链接</a></p>'
        f'<p><b>发布时间：</b>{onenote._x_escape(publish_str or "")}'
        f' &nbsp;|&nbsp; '
        f'<b>推送时间：</b>{push_time}（{trigger_label}）</p>'
        f'<hr />'
    )
    full_body = meta_header + xhtml

    onenote.create_page(
        access_token, section_id,
        final_title, full_body, image_blobs,
        created_iso=art_dt.isoformat(),
    )
    log(f"  ✓ 已推送：{final_title[:60]}")
    # 返回文章页解析到的标题（更权威），用于 state 标题去重
    return title or list_title


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
        log("注意：refresh_token 已更新（在 GitHub Actions 中会通过 gh CLI 自动写回 Secret）")
        try:
            with open(NEW_REFRESH_TOKEN_FILE, "w") as f:
                f.write(new_refresh)
            log(f"  新 token 已写入 {NEW_REFRESH_TOKEN_FILE}（长度 {len(new_refresh)}）供 workflow 后续步骤读取")
        except Exception as e:
            log(f"  ! 写文件失败：{e}；为安全起见，请手动从这次日志找回新 token：")
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
    pushed_urls = set(state.get("pushed_urls", []))
    pushed_titles = set(state.get("pushed_titles", []))
    # 装上单篇超时信号处理（仅 Unix）
    has_signal = hasattr(signal, "SIGALRM")
    if has_signal:
        signal.signal(signal.SIGALRM, _article_timeout_handler)
        # Python 3 默认 SA_RESTART：syscall 被信号中断后自动重启，导致 SIGALRM 不能打断 urlopen.read()
        # siginterrupt(True) 让 syscall 被中断时直接报错，从而能立即终止卡死的下载
        try:
            signal.siginterrupt(signal.SIGALRM, True)
        except Exception:
            pass

    for i, (dt, url, title, content, source) in enumerate(items, 1):
        log(f"[{i}/{len(items)}] {dt} [{source}] {title[:40]}")
        if has_signal:
            signal.alarm(PER_ARTICLE_TIMEOUT_SEC)
        try:
            actual_title = push_one(
                access_token, section_id, dt, url, title, log,
                prefetched_content=content, source_label=source,
            )
            pushed_urls.add(url)
            pushed_titles.add(_norm_title(title))
            if actual_title:
                pushed_titles.add(_norm_title(actual_title))
            n_ok += 1
        except _ArticleTimeout as te:
            log(f"  ! 单篇超时 ({PER_ARTICLE_TIMEOUT_SEC}s)，跳过：{url}")
            # 加入 state 让以后不再重试这篇，避免下次又卡
            pushed_urls.add(url)
            pushed_titles.add(_norm_title(title))
            n_fail += 1
        except Exception:
            err = traceback.format_exc()
            log(f"  ! 失败：\n{err}")
            try:
                if has_signal:
                    signal.alarm(0)
                push_error_page(access_token, section_id, f"文章: {url}\n标题: {title}\n\n{err}")
            except Exception:
                pass
            n_fail += 1
        finally:
            if has_signal:
                signal.alarm(0)
        time.sleep(random.uniform(*DELAY_RANGE))

    state["pushed_urls"] = sorted(pushed_urls)
    state["pushed_titles"] = sorted(pushed_titles)
    save_state(state_path, state)
    log(f"结束。成功 {n_ok}，失败 {n_fail}")
    if n_fail and not n_ok:
        sys.exit(2)  # 全失败：让 Actions 标红


if __name__ == "__main__":
    main()
