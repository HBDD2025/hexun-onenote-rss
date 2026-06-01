# -*- coding: utf-8 -*-
"""和讯保险·行业资讯抓取与解析（精简版，供 daily_push 使用）"""

import gzip
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

UA_LIST = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

LIST_URL = "https://insurance.hexun.com/bxhyzx/index.html"  # 保留以兼容旧调用

# 同步源：5 个和讯保险栏目的列表页
LIST_URLS = [
    "https://insurance.hexun.com/bxhyzx/index.html",  # 行业
    "https://insurance.hexun.com/bxjgdt/index.html",  # 监管
    "https://insurance.hexun.com/bxgsxw/index.html",  # 公司
    "https://insurance.hexun.com/bxzjyy/index.html",  # 保险资金运用
    "https://insurance.hexun.com/bxscpl/",            # 评论与研究
]

# 列表页 URL → 频道名（用于 OneNote 页眉显示"推送自和讯保险的XX频道"）
LIST_CHANNELS = {
    "https://insurance.hexun.com/bxhyzx/index.html": "行业",
    "https://insurance.hexun.com/bxjgdt/index.html": "监管",
    "https://insurance.hexun.com/bxgsxw/index.html": "公司",
    "https://insurance.hexun.com/bxzjyy/index.html": "保险资金运用",
    "https://insurance.hexun.com/bxscpl/":            "评论与研究",
}

RETRIES = 5       # 通用重试次数（文章 HTML / 列表页）。腾讯 EdgeOne 偶发挑战，多试几次有概率命中放行
IMG_RETRIES = 1   # 图片专用：fail fast，下不到就放弃
IMG_TIMEOUT = 15  # 图片单次请求超时（秒）

# jintiankansha cookie（VIP 登录态）。优先从环境变量读，本地也可以读 ~/jintiankansha-cookies.txt
def _load_jintian_cookie():
    import os as _os
    v = _os.environ.get("JINTIANKANSHA_COOKIE", "").strip()
    if v:
        return v
    path = _os.path.expanduser("~/jintiankansha-cookies.txt")
    if _os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""

JINTIAN_COOKIE = _load_jintian_cookie()


# ---------- 代理（仅针对 hexun.com，绕开腾讯 EdgeOne 对 GH Actions IP 的拦截）----------
# 两种格式自动识别：
#   1. 正向代理（Squid / 付费住宅代理）：HEXUN_PROXY_URL=http://[user:pass@]host:port
#      代码用 urllib.request.ProxyHandler 透传
#   2. 反向代理模板（Cloudflare Worker 等）：HEXUN_PROXY_URL=https://your.worker.dev/?url={url}
#      代码会把 {url} 替换成 urlencoded 目标 URL，直接 GET 这个改写后的地址
# 不影响 OneNote / WeChat / jintiankansha 等其他域名
HEXUN_PROXY_URL = os.environ.get("HEXUN_PROXY_URL", "").strip()
_IS_REVERSE_PROXY = "{url}" in HEXUN_PROXY_URL
_PROXY_LOGGED_ONCE = False


def _should_proxy(url):
    return bool(HEXUN_PROXY_URL) and "hexun.com" in url


def _proxy_log_once():
    global _PROXY_LOGGED_ONCE
    if _PROXY_LOGGED_ONCE:
        return
    _PROXY_LOGGED_ONCE = True
    # user:pass 打码再打印
    safe = re.sub(r"://([^@/]+)@", "://***@", HEXUN_PROXY_URL)
    mode = "反向代理模板" if _IS_REVERSE_PROXY else "正向代理"
    print(f"hexun_lib: 启用 {mode}  {safe}", file=sys.stderr, flush=True)


def _open(req, url, timeout):
    """统一开 request；hexun.com 走代理，其他直连。"""
    if not _should_proxy(url):
        return urllib.request.urlopen(req, timeout=timeout)
    _proxy_log_once()
    if _IS_REVERSE_PROXY:
        # 反向代理：把目标 URL urlencoded 后塞进模板，GET 这个改写后的地址
        # 注意 req 自带的 headers (UA/Referer/Cookie) 仍要带上，所以重新建 Request
        import urllib.parse as _up
        rewritten = HEXUN_PROXY_URL.replace("{url}", _up.quote(url, safe=""))
        new_req = urllib.request.Request(rewritten)
        for k, v in req.header_items():
            # Host 一定要丢，让 urllib 用 rewritten URL 自己的 Host
            if k.lower() == "host":
                continue
            new_req.add_header(k, v)
        return urllib.request.urlopen(new_req, timeout=timeout)
    # 正向代理：标准 ProxyHandler
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": HEXUN_PROXY_URL,
            "https": HEXUN_PROXY_URL,
        })
    )
    return opener.open(req, timeout=timeout)


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
    """HTTP GET with anti-bot challenge handling and retry. Returns raw bytes.

    挑战识别两种：
      1. 旧 EO_Bot：< 3KB + `__tst_status`，可本地解（_solve_challenge）
      2. 新腾讯 EdgeOne：含 `__TENCENT_CHAOS_VM`/`TENCENT_CHAOS`，需要 JS VM 才能解，
         本地解不了——只能换 UA + 拉长间隔重试期望命中放行，全部用完就抛错让上层
         logger 看到（避免 parse 出 0 条静默漏推）。
    """
    last_err = None
    cookies = None
    challenge_hits = 0
    for attempt in range(RETRIES):
        try:
            req = _new_request(url, cookies=cookies, referer=referer)
            with _open(req, url, timeout=20) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                # 旧版挑战
                if len(raw) < 3000 and b"__tst_status" in raw:
                    solved = _solve_challenge(raw)
                    if solved:
                        cookies = solved
                        time.sleep(random.uniform(0.5, 1.0))
                        continue
                # 新版腾讯 EdgeOne 虚拟机挑战 — 本地无 JS 解释器，重试期望换 UA/换时机命中放行
                if b"__TENCENT_CHAOS_VM" in raw or b"TENCENT_CHAOS" in raw:
                    challenge_hits += 1
                    print(
                        f"!! hexun_lib.fetch: 收到腾讯 EdgeOne CHAOS_VM 挑战页 "
                        f"({len(raw)}B, attempt={attempt+1}/{RETRIES}) {url}",
                        file=sys.stderr, flush=True,
                    )
                    # 长退避 + 重新走 _new_request（会重新 random.choice UA）
                    cookies = None
                    time.sleep(3 + attempt * 4 + random.uniform(0, 3))
                    continue
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    if challenge_hits:
        raise RuntimeError(
            f"hexun WAF 反复挑战 {challenge_hits} 次（疑似 GitHub Actions IP 被腾讯 "
            f"EdgeOne CHAOS_VM 拦截，无法本地解 JS 挑战）：{url}"
        )
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


def fetch_binary(url, referer=None):
    """Image download — fail fast。最多两次：第一次裸下，遇到 EO_Bot 挑战就解 cookie 再来一次。
    对 jintiankansha.me URL 自动带上 JINTIAN_COOKIE（VIP 登录态，如果配置了的话）。"""
    last_err = None
    cookies = None
    if "jintiankansha.me" in url and JINTIAN_COOKIE:
        cookies = JINTIAN_COOKIE
    # 最多两次：一次裸 + 一次带 cookie
    for attempt in range(IMG_RETRIES + 1):
        try:
            req = _new_request(url, cookies=cookies, referer=referer)
            with _open(req, url, timeout=IMG_TIMEOUT) as r:
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
