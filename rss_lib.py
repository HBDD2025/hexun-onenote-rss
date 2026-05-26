# -*- coding: utf-8 -*-
"""
解析 RSS 2.0 订阅源（如 jintiankansha.me 的微信公众号 RSS）。
直接用 stdlib xml.etree，避免引入 feedparser 依赖。
"""

import email.utils
import gzip
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET


BEIJING = timezone(timedelta(hours=8))

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _fetch_xml(url, timeout=30):
    req = urllib.request.Request(url)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    req.add_header("Accept", "application/rss+xml, application/xml, text/xml, */*")
    req.add_header("Accept-Encoding", "gzip, deflate")
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
    raise RuntimeError(f"RSS 拉取失败：{url}：{last_err}")


def _text(elem, tag, ns=None):
    if ns:
        node = elem.find(tag, ns)
    else:
        node = elem.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text


def _parse_pubdate(s):
    """RFC 822 → datetime(北京时区)。失败时返回 None。"""
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING)
        return dt.astimezone(BEIJING)
    except Exception:
        return None


def get_channel_link(url):
    """从 RSS feed 里抓 <channel><link> ——即专栏在 jintiankansha.me 的主页 URL。"""
    raw = _fetch_xml(url)
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if channel is None:
        return ""
    link = channel.find("link")
    title = channel.find("title")
    return (link.text or "").strip(), (title.text or "").strip() if (title is not None and link is not None) else ("", "")


# 文章页解析（用于 backfill 时从 listing 跳到文章）
_TITLE_RE = re.compile(r"<h1>(.*?)</h1>", re.S | re.I)
_DATETIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")
_BODY_OPEN_RE = re.compile(r'<div\s+class="rich_media_content[^"]*"[^>]*>', re.I)
_DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.I)


def parse_article_page(html):
    """jintiankansha 单篇文章页 → (title, dt_with_beijing_tz, content_html)。"""
    mt = _TITLE_RE.search(html)
    title = re.sub(r"<[^>]+>", "", mt.group(1)).strip() if mt else None

    md = _DATETIME_RE.search(html)
    dt = None
    if md:
        try:
            dt = datetime.strptime(f"{md.group(1)} {md.group(2)}", "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=BEIJING)
        except Exception:
            dt = None

    mb = _BODY_OPEN_RE.search(html)
    content = None
    if mb:
        start = mb.end()
        depth = 1
        pos = start
        while pos < len(html):
            mm = _DIV_TAG_RE.search(html, pos)
            if not mm:
                break
            if mm.group(1):
                depth -= 1
                if depth == 0:
                    content = html[start:mm.start()]
                    break
            else:
                depth += 1
            pos = mm.end()
    return title, dt, content


_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_html(url, timeout=20, cookie=None, referer=None):
    """通用 HTML 拉取（与 _fetch_xml 类似，但 Accept 是 text/html）。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _DEFAULT_UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
    req.add_header("Accept-Encoding", "gzip, deflate")
    if cookie:
        req.add_header("Cookie", cookie)
    if referer:
        req.add_header("Referer", referer)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
    raise RuntimeError(f"HTML 拉取失败：{url}：{last_err}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_jintian_to_wechat(t_id, cookie, timeout=20):
    """
    把 jintiankansha 的 /t/XXX 标识解析成真实 mp.weixin.qq.com URL。
    需要 VIP cookie；/t_original/XXX 走 302 跳转。
    """
    url = f"http://www.jintiankansha.me/t_original/{t_id}"
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _DEFAULT_UA)
    req.add_header("Accept", "*/*")
    req.add_header("Cookie", cookie)
    try:
        r = opener.open(req, timeout=timeout)
        loc = r.headers.get("Location")
        r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
        else:
            raise RuntimeError(f"/t_original/{t_id} HTTP {e.code}")
    if not loc:
        # 没 302 一般意味着登录失效或 VIP 已过期
        raise RuntimeError(f"/t_original/{t_id} 无重定向（VIP/cookie 可能失效）")
    if "mp.weixin.qq.com" not in loc and "weixin.qq.com" not in loc:
        raise RuntimeError(f"重定向到非微信地址：{loc[:100]}")
    return loc


# WeChat 文章页解析
_WX_TITLE_RE = re.compile(
    r'<h[12][^>]*(?:class=["\']rich_media_title|id=["\']activity-name)[^>]*>(.*?)</h[12]>',
    re.S | re.I,
)
_WX_DATE_RE_1 = re.compile(r'id=["\']publish_time["\'][^>]*>\s*(\d{4}-\d{2}-\d{2})\s*<')
_WX_DATE_RE_2 = re.compile(r'(?:var\s+publish_time|publish_time)\s*=\s*["\'](\d{4}-\d{2}-\d{2})')
_WX_DATE_RE_3 = re.compile(r'<em[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</em>')
_WX_DATE_TS_RE = re.compile(r'var\s+ct\s*=\s*["\'](\d{10})["\']')
_WX_BODY_OPEN_RE = re.compile(r'<div\s+(?:class="rich_media_content[^"]*"\s+id="js_content"|id="js_content"[^>]*class="rich_media_content)', re.I)


def parse_wechat_article(html):
    """
    解析 mp.weixin.qq.com 文章页 → (title, dt_with_beijing_tz, content_html)。
    日期来源：publish_time DOM 标记 或 var ct=unix_timestamp。
    """
    mt = _WX_TITLE_RE.search(html)
    title = None
    if mt:
        title = re.sub(r"<[^>]+>", "", mt.group(1)).strip()

    dt = None
    for rx in (_WX_DATE_RE_1, _WX_DATE_RE_2, _WX_DATE_RE_3):
        m = rx.search(html)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=BEIJING)
                break
            except Exception:
                continue
    if dt is None:
        m = _WX_DATE_TS_RE.search(html)
        if m:
            try:
                ts = int(m.group(1))
                dt = datetime.fromtimestamp(ts, tz=BEIJING)
            except Exception:
                pass

    content = None
    m = re.search(r'<div\s+[^>]*(class="rich_media_content[^"]*"\s+id="js_content"|id="js_content"[^>]*class="rich_media_content[^"]*")[^>]*>', html, re.I)
    if m:
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
                    content = html[start:mm.start()]
                    break
            else:
                depth += 1
            pos = mm.end()

    return title, dt, content


def t_id_from_url(t_url):
    """从 http://www.jintiankansha.me/t/XXXXX 提取 XXXXX。"""
    return t_url.rstrip("/").rsplit("/", 1)[-1]


def parse_feed(url):
    """
    返回 (channel_title, items)
    items: list of dict {title, link, pubdate(datetime|None), date(date|None), content_html, source}
    """
    raw = _fetch_xml(url)
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if channel is None:
        return "", []
    chan_title = _text(channel, "title").strip()

    items = []
    for it in channel.findall("item"):
        title = _text(it, "title").strip()
        link = _text(it, "link").strip()
        pub_raw = _text(it, "pubDate")
        pub_dt = _parse_pubdate(pub_raw)
        # 内容优先 content:encoded，回退 description
        content = ""
        node = it.find("content:encoded", NS)
        if node is not None and node.text:
            content = node.text
        if not content:
            content = _text(it, "description")
        items.append({
            "title": title,
            "link": link,
            "pubdate": pub_dt,
            "date": pub_dt.date() if pub_dt else None,
            "content_html": content,
            "source": chan_title,
        })
    return chan_title, items


# 14 个 RSS 源
FEEDS = [
    "http://rss.jintiankansha.me/rss/GMZTQMZZHB6DMOJUGJSWIYJZMVTGEZDCMUZWGZJQMRQTMZRQGNQTEZDCHEZTGNRWMJQTIZRRGI2A====",
    "http://rss.jintiankansha.me/rss/GMZTOOJSGB6DOZLCGQZWCZRQGA2TEY3CGMZWCYJWGQ3TKYJTMRRDKM3FMJRDOZJUGYYDSMBUMQZQ====",
    "http://rss.jintiankansha.me/rss/GM2DANJSGF6GCNLBGA3DIZRVMZQTSMRYGI4DOOJUMY4WEY3CMZQTSYJZGRRGKNBUGI2GEZDCMVRQ====",
    "http://rss.jintiankansha.me/rss/GEYDANZXHF6DCMDBMZSDGMDDMRTDMZRYGIYDANTEGA3TMYRTGAYGINLEGYZDIMLBGQYWEMBTHAZQ====",
    "http://rss.jintiankansha.me/rss/GEYDAOJZGB6DGNZRGBQTEMJQGMYGGYZVMU2DIOLDGQYDAN3EGRRTMZRVHA4DCZLFGAYWEMLDMIYA====",
    "http://rss.jintiankansha.me/rss/GMZTONZSGR6DMMZXGA4DQMZWGE3TAMTDMEYGGOJTGRSGMNLBGM4GGY3CGAZGENLGGQ3TMN3GMMZQ====",
    "http://rss.jintiankansha.me/rss/GMZTMNBRHB6DOMDBGFRTCZDCGRQTOMTDMY3TMMBSME2DQMRWGUZWMNLBMUYDCZRRG44DINZXHE3Q====",
    "http://rss.jintiankansha.me/rss/GEYDQMJQHF6DQOJRMZTDIZTGHAYDMZJSMYYWMOBUGM3GEZTGMQ2DIMBRHA3GGNLEGFSDENDEMEYQ====",
    "http://rss.jintiankansha.me/rss/GMZTONZSGN6DMOBSHFSDSMLEGQYTEZBRHBRTQNDCGMZWIMBTGYYDOMBWG44DQY3FHAZGMOJUMI4A====",
    "http://rss.jintiankansha.me/rss/GM2TSOBSGJ6GINBSMJRGIYRYGNQWKNBZGY4DMNLEGIZTMMRRGRRTENBXHFQWKN3FMMYDAYRVGJTA====",
    "http://rss.jintiankansha.me/rss/GMZTONZSGB6GMNZWG43TENZZMEYGMNRXMMYTKMBSGQ4DSMJZGU4DEODCGA4DQZJYGM4DAYRYMMYQ====",
    "http://rss.jintiankansha.me/rss/GM2DCOBTGJ6DKY3DMFTGEMRYMFQWENRUGAZTEYRTMYZGENDFGA2DQYJTGI3DOY3CGNQTMOJYMM4A====",
    # lanjingbaoxian (蓝鲸保险) 已按用户要求移除
    "http://rss.jintiankansha.me/rss/GMZTONZRHF6DQZJSMY4GIMJQG5TGMZBWHA4WIMJWGUYTKNBYG5QTKYJVMQYGINJYGAYDEODEGFTA====",
]
