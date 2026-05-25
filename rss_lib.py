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
    "http://rss.jintiankansha.me/rss/GMZTSNZUGJ6GMMRRHFSTCMZWGUYDGZTCHFTGCZTGG4YTIYZVMMZDMMZQMFTGGNJVMQ2TEOBZMVSQ====",
    "http://rss.jintiankansha.me/rss/GMZTONZRHF6DQZJSMY4GIMJQG5TGMZBWHA4WIMJWGUYTKNBYG5QTKYJVMQYGINJYGAYDEODEGFTA====",
]
