#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hexun_scraper.py — 抓取和讯保险 5 个栏目的新闻文章
（行业资讯 / 监管动态 / 公司新闻 / 中介营销 / 市场评论）
按用户输入的日期范围，把所有文章正文汇总到一个 TXT 文件。
跨栏目的同一篇新闻会按 URL + 归一化标题去重，只保留一条。
图片/图表/表格在原始 DOM 位置插入【此处有图片或图表】占位符。
"""

import gzip
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser


# ----------------------------- 常量 -----------------------------

UA_LIST = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

# 5 个栏目配置：每条 (slug, 中文名, index 后缀, 数字翻页范围 (low, high)，None=不翻页)
SECTIONS = [
    ("bxhyzx", "行业资讯", "index.html", (520, 700)),  # 已知 668 是次新数字页
    ("bxjgdt", "监管动态", "index.html", None),          # 实测无数字翻页
    ("bxgsxw", "公司新闻", "index.html", (590, 700)),    # 已知 679 是次新数字页
    ("bxzjyy", "中介营销", "index.html", None),          # 单页含 2020+ 全部
    ("bxscpl", "市场评论", "",           None),          # 单页含 2024+ 全部
]
# bxhyzx 翻页页号到日期的粗对照：520→2018-01 / 545→2018-11 / 570→2019-12 / 594→2020-12
# bxgsxw 翻页页号到日期的粗对照：600→2019-03 / 640→2021-12 / 660→2023-07 / 679→2026-01

IMG_PLACEHOLDER = "【此处有图片或图表】"

DELAY_MIN = 1.0
DELAY_MAX = 3.0
RETRIES = 3


# ----------------------------- 网络层 -----------------------------

def _new_request(url, cookies=None, referer=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", random.choice(UA_LIST))
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Encoding", "gzip, deflate")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    req.add_header("Connection", "keep-alive")
    if referer:
        req.add_header("Referer", referer)
    if cookies:
        req.add_header("Cookie", cookies)
    return req


def _solve_challenge(raw_bytes):
    """和讯/EO_Bot JS 反爬挑战解算：从挑战页 JS 里提取 4 个数字，算出 cookies。"""
    s = raw_bytes.decode("latin1", errors="ignore")
    if "__tst_status" not in s:
        return None
    m_ssid = re.search(r"\(t,(\d+)\)", s)
    if not m_ssid:
        return None
    ssid = m_ssid.group(1)
    nums = [int(x.group(1)) for x in re.finditer(r"(?:WTKkN|bOYDu|wyeCN)\s*:\s*(\d+)", s)]
    if len(nums) != 3:
        return None
    tst = sum(nums)
    return f"__tst_status={tst}#; EO_Bot_Ssid={ssid}"


def fetch(url, referer=None):
    """带重试 + JS 挑战兜底的下载。返回原始字节。"""
    last_err = None
    cookies = None
    for attempt in range(RETRIES):
        try:
            req = _new_request(url, cookies=cookies, referer=referer)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                # 反爬挑战：小响应 + 含 __tst_status 关键字
                if len(raw) < 3000 and b"__tst_status" in raw:
                    solved = _solve_challenge(raw)
                    if solved:
                        cookies = solved
                        time.sleep(random.uniform(0.6, 1.2))
                        continue  # 用新 cookie 重试
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"下载失败：{url}：{last_err}")


def decode_html(raw):
    """和讯主要是 gb2312/gbk，少数页可能是 utf-8。"""
    head = raw[:1024].decode("latin1", errors="ignore").lower()
    if "utf-8" in head or "utf8" in head:
        candidates = ("utf-8", "gb18030")
    else:
        candidates = ("gb18030", "utf-8")
    for enc in candidates:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


# ----------------------------- 列表页解析 -----------------------------

LIST_RE = re.compile(
    r'<li[^>]*>\s*<span[^>]*>\(\d{2}-\d{2}\s+\d{2}:\d{2}\)</span>\s*'
    r'<a[^>]+href=["\'](https?://insurance\.hexun\.com/(\d{4})-(\d{2})-(\d{2})/(\d+)\.html)["\'][^>]*>'
    r'([^<]+)</a>',
    re.I,
)


def parse_list_page(html):
    """从列表页 HTML 中抽出条目 [(date, url, title), ...]，限定主列表区的 <li> 结构。"""
    out = []
    for m in LIST_RE.finditer(html):
        url = m.group(1).replace("http://", "https://")
        try:
            dt = date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        title = m.group(6).strip()
        out.append((dt, url, title))
    return out


# ----------------------------- 文章页解析 -----------------------------

TITLE_RE = re.compile(r'<div\s+class="articleName"[^>]*>\s*<h1[^>]*>(.*?)</h1>', re.S | re.I)
TIME_RE = re.compile(r'<span\s+class="pr20"[^>]*>\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*</span>', re.I)
SOURCE_RE = re.compile(
    r'<span\s+class="pr20"[^>]*>[^<]*</span>\s*'
    r'(?:<a[^>]*>([^<]+)</a>|([^<&\s][^<]*?)(?=\s*[<&]))',
    re.I | re.S,
)

DISCLAIMER_PATTERNS = [
    re.compile(r"\n*（责任编辑[:：][^\n）]+）.*$", re.S),
    re.compile(r"\n*【免责声明】.*$", re.S),
    re.compile(r"\n*免责声明[:：].*$", re.S),
]


class BodyParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section"}
    SKIP_TAGS = {"script", "style", "noscript", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0
        self.in_table = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "img":
            self.parts.append("\n\n" + IMG_PLACEHOLDER + "\n\n")
        elif tag == "table":
            self.parts.append("\n\n" + IMG_PLACEHOLDER + "\n\n")
            self.in_table += 1
            self.skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        # 处理 <img />、<br/> 自闭合
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if tag == "table" and self.in_table:
            self.in_table -= 1
            if self.skip_depth:
                self.skip_depth -= 1
            self.parts.append("\n\n")
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        # 把各种空白（含 \xa0 = nbsp）压成普通空格，但保留换行
        cleaned_chars = []
        for ch in data:
            if ch == "\n":
                cleaned_chars.append("\n")
            elif ch.isspace():
                cleaned_chars.append(" ")
            else:
                cleaned_chars.append(ch)
        s = "".join(cleaned_chars)
        s = re.sub(r" +", " ", s)
        if s.strip():
            self.parts.append(s)

    def get_text(self):
        text = "".join(self.parts)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [ln.rstrip() for ln in text.split("\n")]
        return "\n".join(lines).strip()


def extract_body(html):
    """定位 <div class="art_contextBox">...</div>，深度计数匹配收尾标签。"""
    m = re.search(r'<div\s+class="art_contextBox"[^>]*>', html, re.I)
    if not m:
        return None
    start = m.end()
    depth = 1
    pos = start
    rx = re.compile(r"<(/?)div\b[^>]*>", re.I)
    while pos < len(html):
        mm = rx.search(html, pos)
        if not mm:
            break
        if mm.group(1):
            depth -= 1
            if depth == 0:
                inner = html[start:mm.start()]
                bp = BodyParser()
                bp.feed(inner)
                text = bp.get_text()
                for pat in DISCLAIMER_PATTERNS:
                    text = pat.sub("", text)
                return text.strip()
        else:
            depth += 1
        pos = mm.end()
    return None


def parse_article(html):
    title = None
    mt = TITLE_RE.search(html)
    if mt:
        title = re.sub(r"<[^>]+>", "", mt.group(1)).strip()
    mtm = TIME_RE.search(html)
    publish = mtm.group(1) if mtm else None
    msrc = SOURCE_RE.search(html)
    source = None
    if msrc:
        source = (msrc.group(1) or msrc.group(2) or "").strip()
        source = re.sub(r"\s+", " ", source)
    body = extract_body(html)
    return title, publish, source, body


# ----------------------------- 主流程 -----------------------------

def _norm_title(t):
    """标题归一化：去全部空白，用作跨栏目去重的 key。"""
    return re.sub(r"\s+", "", t or "")


def _walk_section(slug, suffix, page_range, start_d, end_d, log):
    """遍历单个栏目的列表页（index + 数字页），吐出区间内条目。"""
    out = []
    # 1. index.html / 空后缀
    first_url = f"https://insurance.hexun.com/{slug}/{suffix}"
    log(f"  [{slug}/index] 拉取...")
    try:
        raw = fetch(first_url, referer="https://insurance.hexun.com/")
        entries = parse_list_page(decode_html(raw))
    except Exception as e:
        log(f"    ! 拉取失败：{e}")
        return out
    if entries:
        dmax = max(e[0] for e in entries)
        dmin = min(e[0] for e in entries)
        in_range = [(d, u, t) for d, u, t in entries if start_d <= d <= end_d]
        log(f"    {len(entries)} 条 [{dmin}~{dmax}]，入选 {len(in_range)}")
        out.extend(in_range)
        # 早停：本页最新已早于起始日期
        if dmax < start_d:
            return out
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # 2. 数字翻页
    if not page_range:
        return out
    low, high = page_range
    consec_empty = 0
    found_any = False
    for n in range(high, low - 1, -1):
        url = f"https://insurance.hexun.com/{slug}/index-{n}.html"
        try:
            raw = fetch(url, referer="https://insurance.hexun.com/")
            entries = parse_list_page(decode_html(raw))
        except Exception as e:
            log(f"    ! {slug}/index-{n} 失败：{e}")
            time.sleep(random.uniform(0.5, 1.0))
            continue
        if not entries:
            consec_empty += 1
            if found_any and consec_empty >= 5:
                log(f"  [{slug}/index-{n}] 连续空页，停止翻页")
                break
            time.sleep(random.uniform(0.5, 1.0))
            continue
        consec_empty = 0
        found_any = True
        dmax = max(e[0] for e in entries)
        dmin = min(e[0] for e in entries)
        in_range = [(d, u, t) for d, u, t in entries if start_d <= d <= end_d]
        log(f"  [{slug}/index-{n}] {len(entries)} 条 [{dmin}~{dmax}]，入选 {len(in_range)}")
        out.extend(in_range)
        if dmax < start_d:
            log(f"  本页最新 {dmax} < 起始日期，停止翻页")
            break
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    return out


def collect_urls(start_d, end_d, log):
    """5 个栏目全部扫一遍，按 URL + 归一化标题去重。"""
    all_entries = []
    for slug, name, suffix, page_range in SECTIONS:
        log(f"\n=== {slug} ({name}) ===")
        all_entries.extend(_walk_section(slug, suffix, page_range, start_d, end_d, log))

    log(f"\n汇总（含跨栏目重复）：{len(all_entries)} 条")
    # URL 去重
    seen_url = set()
    after_url = []
    for tup in all_entries:
        if tup[1] in seen_url:
            continue
        seen_url.add(tup[1])
        after_url.append(tup)
    log(f"URL 去重 → {len(after_url)} 条")
    # 归一化标题去重（保留首次出现）
    seen_t = set()
    deduped = []
    title_dups = 0
    for dt, url, title in after_url:
        k = _norm_title(title)
        if k and k in seen_t:
            title_dups += 1
            continue
        if k:
            seen_t.add(k)
        deduped.append((dt, url, title))
    log(f"标题去重 → {len(deduped)} 条（跨栏目同标题 {title_dups} 条）")
    return deduped


def prompt(label, default=None):
    suffix = f"（回车 = {default}）" if default is not None else ""
    raw = input(f"{label}{suffix}：").strip()
    return raw or (default or "")


def prompt_date(label, default):
    while True:
        raw = prompt(label, default)
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  格式不对，请用 YYYY-MM-DD")


def safe_filename(name):
    return re.sub(r'[/\\:*?"<>|]', "_", name)


def main():
    print("=" * 64)
    print("和讯保险 5 栏目批量抓取（行业资讯 / 监管动态 / 公司新闻 / 中介营销 / 市场评论）")
    print("可抓时段：约 2018-01 至今；跨栏目同标题自动去重，只保留一条")
    print("=" * 64)

    today = date.today()
    start_d = prompt_date("起始日期 YYYY-MM-DD", "2021-01-01")
    end_d = prompt_date("结束日期 YYYY-MM-DD", today.isoformat())
    if start_d > end_d:
        print("起始日期晚于结束日期，退出。")
        return

    default_out = os.path.expanduser(
        f"~/Desktop/和讯保险_{start_d}_至_{end_d}.txt"
    )
    out_path = prompt("输出文件路径", default_out)
    out_path = os.path.expanduser(out_path)

    if os.path.exists(out_path):
        ans = input(f"文件已存在：{out_path}\n覆盖？(y/N)：").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    def log(msg):
        print(msg, flush=True)

    log(f"\n阶段 1/2：扫描列表页，筛选 {start_d} ~ {end_d} 区间的文章 URL")
    log("-" * 64)
    items = collect_urls(start_d, end_d, log)
    items.sort(key=lambda x: x[0])  # 时间升序
    log(f"\n共找到 {len(items)} 篇待抓取。")
    if not items:
        log("该日期范围没有匹配文章。")
        input("\n按回车关闭...")
        return
    avg_sec = (DELAY_MIN + DELAY_MAX) / 2 + 0.8  # 延时 + 平均下载时间
    est_min = len(items) * avg_sec / 60
    log(f"预计耗时 ~{est_min:.1f} 分钟（约 {avg_sec:.1f} 秒/篇）")
    if len(items) > 50:
        ans = input("继续？(Y/n)：").strip().lower()
        if ans == "n":
            log("已取消。")
            return

    log(f"\n阶段 2/2：抓取文章正文，写入 {out_path}")
    log("-" * 64)
    n_ok = 0
    n_fail = 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"和讯保险·行业资讯 ({start_d} ~ {end_d})\n")
        f.write(f"共 {len(items)} 篇\n")
        f.write("=" * 70 + "\n\n")
        for i, (dt, url, list_title) in enumerate(items, 1):
            log(f"[{i}/{len(items)}] {dt}  {url}")
            try:
                raw = fetch(url, referer="https://insurance.hexun.com/")
                html = decode_html(raw)
                title, publish, source, body = parse_article(html)
            except Exception as e:
                log(f"   ! 下载失败：{e}")
                n_fail += 1
                f.write(f"[抓取失败] {dt}  {url}\n   原因：{e}\n\n{'=' * 70}\n\n")
                continue
            if not body:
                log("   ! 未解析到正文（可能结构变化）")
                n_fail += 1
                f.write(f"[正文解析失败] {dt}  {url}\n\n{'=' * 70}\n\n")
                continue
            f.write(f"标题：{title or list_title}\n")
            f.write(f"时间：{publish or dt}\n")
            f.write(f"来源：{source or ''}\n")
            f.write(f"链接：{url}\n")
            f.write("-" * 70 + "\n")
            f.write(body + "\n\n")
            f.write("=" * 70 + "\n\n")
            f.flush()
            n_ok += 1
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    log("\n" + "=" * 64)
    log(f"完成：成功 {n_ok} 篇，失败 {n_fail} 篇")
    log(f"输出文件：{out_path}")
    log("=" * 64)
    input("\n按回车关闭窗口...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        input("\n按回车关闭...")
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n出错了，按回车关闭...")
