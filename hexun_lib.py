# -*- coding: utf-8 -*-
"""和讯保险·行业资讯抓取与解析（精简版，供 daily_push 使用）"""

import gzip
import random
import re
import time
import urllib.error
import urllib.request
from datetime import date

UA_LIST = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

LIST_URL = "https://insurance.hexun.com/bxhyzx/index.html"  # 保留以兼容旧调用

# 同步源：5 个和讯保险栏目的列表页
LIST_URLS = [
    "https://insurance.hexun.com/bxhyzx/index.html",  # 行业资讯
    "https://insurance.hexun.com/bxjgdt/index.html",  # 监管动态
    "https://insurance.hexun.com/bxgsxw/index.html",  # 公司新闻
    "https://insurance.hexun.com/bxzjyy/index.html",  # 中介营销
    "https://insurance.hexun.com/bxscpl/",            # 市场评论
]

RETRIES = 3       # 通用重试次数（文章 HTML / 列表页）
IMG_RETRIES = 1   # 图片专用：fail fast，下不到就放弃
IMG_TIMEOUT = 12  # 图片单次请求超时（秒）


def _new_request(url, cookies=None, referer=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", random.choice(UA_LIST))
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Encoding", "gzip, deflate")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    if referer:
        req.add_header("Referer", referer)
    if cookies:
        req.add_header("Cookie", cookies)
    return req


def _solve_challenge(raw_bytes):
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
    return f"__tst_status={sum(nums)}#; EO_Bot_Ssid={ssid}"


def fetch(url, referer=None):
    """HTTP GET with anti-bot challenge handling and retry. Returns raw bytes."""
    last_err = None
    cookies = None
    for attempt in range(RETRIES):
        try:
            req = _new_request(url, cookies=cookies, referer=referer)
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if len(raw) < 3000 and b"__tst_status" in raw:
                    solved = _solve_challenge(raw)
                    if solved:
                        cookies = solved
                        time.sleep(random.uniform(0.5, 1.0))
                        continue
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


def fetch_binary(url, referer=None):
    """Image download — fail fast。最多两次：第一次裸下，遇到 EO_Bot 挑战就解 cookie 再来一次。"""
    last_err = None
    cookies = None
    # 最多两次：一次裸 + 一次带 cookie
    for attempt in range(IMG_RETRIES + 1):
        try:
            req = _new_request(url, cookies=cookies, referer=referer)
            with urllib.request.urlopen(req, timeout=IMG_TIMEOUT) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
                if (len(raw) < 3000 and b"__tst_status" in raw
                        and ctype.startswith("text/") and cookies is None):
                    solved = _solve_challenge(raw)
                    if solved:
                        cookies = solved
                        time.sleep(random.uniform(0.3, 0.7))
                        continue
                return raw, ctype
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            break  # 图片不再多次重试，节省时间
    raise RuntimeError(f"image fetch failed: {url}: {last_err}")


def decode_html(raw):
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


LIST_RE = re.compile(
    r'<li[^>]*>\s*<span[^>]*>\((\d{2})-(\d{2})\s+(\d{2}):(\d{2})\)</span>\s*'
    r'<a[^>]+href=["\'](https?://insurance\.hexun\.com/(\d{4})-(\d{2})-(\d{2})/(\d+)\.html)["\'][^>]*>'
    r'([^<]+)</a>',
    re.I,
)


def parse_list_page(html):
    """Returns list of (date_obj, url, title). De-duplicated within the page."""
    seen = set()
    out = []
    for m in LIST_RE.finditer(html):
        url = m.group(5).replace("http://", "https://")
        if url in seen:
            continue
        seen.add(url)
        try:
            dt = date(int(m.group(6)), int(m.group(7)), int(m.group(8)))
        except ValueError:
            continue
        out.append((dt, url, m.group(10).strip()))
    return out


TITLE_RE = re.compile(r'<div\s+class="articleName"[^>]*>\s*<h1[^>]*>(.*?)</h1>', re.S | re.I)
TIME_RE = re.compile(r'<span\s+class="pr20"[^>]*>\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*</span>', re.I)
SOURCE_RE = re.compile(
    r'<span\s+class="pr20"[^>]*>[^<]*</span>\s*'
    r'(?:<a[^>]*>([^<]+)</a>|([^<&\s][^<]*?)(?=\s*[<&]))',
    re.I | re.S,
)


def extract_article_meta(html):
    """Returns (title, publish_str, source)."""
    title = None
    mt = TITLE_RE.search(html)
    if mt:
        title = re.sub(r"<[^>]+>", "", mt.group(1)).strip()
    mtm = TIME_RE.search(html)
    publish = mtm.group(1) if mtm else None
    msrc = SOURCE_RE.search(html)
    source = ""
    if msrc:
        source = (msrc.group(1) or msrc.group(2) or "").strip()
        source = re.sub(r"\s+", " ", source)
    return title, publish, source


def extract_body_html(html):
    """Return the raw inner HTML of <div class="art_contextBox">…</div>."""
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
                return html[start:mm.start()]
        else:
            depth += 1
        pos = mm.end()
    return None
