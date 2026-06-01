#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读 feed_items.json，生成 kindle.epub，并通过 SMTP 邮件发到 Kindle 邮箱。

环境变量：
  KINDLE_EMAIL    必填，形如 yourname_xxxxxx@kindle.com
  SMTP_HOST       必填，如 smtp.qq.com / smtp.gmail.com / smtp.163.com
  SMTP_PORT       默认 465（SSL）；587 = STARTTLS
  SMTP_USER       发件人邮箱完整地址
  SMTP_PASS       SMTP 授权码或应用专用密码（不是登录密码！）

Amazon Send-to-Kindle 要求：
  1. 必须把 SMTP_USER 这个发件人邮箱地址，加入 Amazon 账户的
     "已认可发件人邮箱" 列表（Approved Personal Document E-mail List）
  2. EPUB 附件大小限制 50MB；我们大概 < 1MB，毫无压力
"""

import json
import os
import re
import smtplib
import ssl
import sys
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.sax.saxutils import escape, quoteattr


BEIJING = timezone(timedelta(hours=8))

FEED_FILE = os.environ.get("FEED_ITEMS_FILE", "feed_items.json")
OUT_EPUB = "kindle.epub"

BOOK_TITLE_BASE = "保险行业聚合"
BOOK_AUTHOR = "和讯保险 + 公众号"
BOOK_LANG = "zh-CN"


# ---------- EPUB 构建（纯 stdlib，无第三方依赖） ----------

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _clean_xhtml(html_str):
    """让 content_html 尽量符合 XHTML：
    - 把 <br> / <hr> / <img ...> 等空标签补成自闭合
    - 数字字符引用替换不需要做（HTMLParser 已转）
    """
    if not html_str:
        return ""
    # <br>, <br /> 等都规范成 <br />
    html_str = re.sub(r"<br\s*/?>", "<br />", html_str, flags=re.I)
    html_str = re.sub(r"<hr\s*/?>", "<hr />", html_str, flags=re.I)
    # <img ...> 没自闭合的强制补
    html_str = re.sub(r"<img\b([^>]*?)(?<!/)>", r"<img\1 />", html_str, flags=re.I)
    return html_str


def build_chapter_xhtml(item):
    title = item.get("title", "(无标题)")
    source = item.get("source", "")
    pubdate = (item.get("pubdate_iso", "") or "")[:10]
    content_html = _clean_xhtml(item.get("content_html", ""))

    # 注意：content_html 里已经被 daily_push 加了 meta 块（频道/来源/发布时间/推送时间）
    # 所以这里只补一个章节大标题
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>{escape(title)}</title>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>
<style type="text/css">
body {{ font-family: serif; line-height: 1.7; margin: 1em; }}
h1.title {{ font-size: 1.4em; margin: 0.4em 0 0.6em 0; }}
img {{ max-width: 100%; height: auto; }}
hr {{ border: none; border-top: 1px solid #888; margin: 1em 0; }}
p {{ margin: 0.4em 0; }}
.meta {{ color: #666; font-size: 0.9em; }}
</style>
</head>
<body>
<h1 class="title">{escape(title)}</h1>
<p class="meta">[{escape(source)}] {escape(pubdate)}</p>
{content_html}
</body>
</html>
"""


def build_nav_xhtml(items):
    lis = []
    for i, it in enumerate(items):
        title = it.get("title", "(无标题)")
        source = it.get("source", "")
        lis.append(
            f'        <li><a href="ch{i}.xhtml">[{escape(source)}] {escape(title)}</a></li>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<title>目录</title>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>
</head>
<body>
<nav epub:type="toc" id="toc">
  <h1>目录</h1>
  <ol>
{chr(10).join(lis)}
  </ol>
</nav>
</body>
</html>
"""


def build_ncx(items, book_id):
    """EPUB 2 旧式目录（兼容老 Kindle 设备）。"""
    nav_points = []
    for i, it in enumerate(items):
        title = it.get("title", "(无标题)")
        nav_points.append(
            f'  <navPoint id="nav{i}" playOrder="{i+1}">\n'
            f'    <navLabel><text>{escape(title)}</text></navLabel>\n'
            f'    <content src="ch{i}.xhtml"/>\n'
            f'  </navPoint>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head>
  <meta name="dtb:uid" content="{escape(book_id)}"/>
  <meta name="dtb:depth" content="1"/>
  <meta name="dtb:totalPageCount" content="0"/>
  <meta name="dtb:maxPageNumber" content="0"/>
</head>
<docTitle><text>{escape(BOOK_TITLE_BASE)}</text></docTitle>
<navMap>
{chr(10).join(nav_points)}
</navMap>
</ncx>
"""


def build_opf(items, book_id, book_title):
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    spine = []
    for i in range(len(items)):
        manifest.append(
            f'    <item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'    <itemref idref="ch{i}"/>')

    modified_utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="bookid">{escape(book_id)}</dc:identifier>
  <dc:title>{escape(book_title)}</dc:title>
  <dc:creator>{escape(BOOK_AUTHOR)}</dc:creator>
  <dc:language>{BOOK_LANG}</dc:language>
  <meta property="dcterms:modified">{modified_utc}</meta>
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine toc="ncx">
{chr(10).join(spine)}
</spine>
</package>
"""


def build_epub(items, out_path):
    now_bj = datetime.now(BEIJING)
    book_title = f"{BOOK_TITLE_BASE} · {now_bj.strftime('%Y-%m-%d')}"
    book_id = f"urn:uuid:{uuid.uuid4()}"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # EPUB 规范：mimetype 必须是 zip 里第一个文件，且不压缩
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/content.opf", build_opf(items, book_id, book_title))
        z.writestr("OEBPS/toc.ncx", build_ncx(items, book_id))
        z.writestr("OEBPS/nav.xhtml", build_nav_xhtml(items))
        for i, it in enumerate(items):
            z.writestr(f"OEBPS/ch{i}.xhtml", build_chapter_xhtml(it))
    return book_title


# ---------- SMTP 发送 ----------

def send_via_smtp(to_addr, subject, epub_path, smtp_host, smtp_port,
                  smtp_user, smtp_pass):
    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(
        "由 hexun-onenote-rss workflow 自动推送。\n"
        "见附件 EPUB，Send-to-Kindle 自动同步到 Kindle 设备。",
        "plain", "utf-8",
    ))

    with open(epub_path, "rb") as f:
        att = MIMEApplication(f.read(), _subtype="epub+zip")
    filename = f"hexun-{datetime.now(BEIJING).strftime('%Y%m%d')}.epub"
    att.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(att)

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)


# ---------- 入口 ----------

def main():
    kindle_email = os.environ.get("KINDLE_EMAIL", "").strip()
    if not kindle_email:
        print("KINDLE_EMAIL 未配置，跳过 Kindle 推送", flush=True)
        return 0

    for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        if not os.environ.get(v):
            print(f"!! {v} 未配置，无法发邮件", file=sys.stderr)
            return 2
    smtp_host = os.environ["SMTP_HOST"].strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ["SMTP_USER"].strip()
    smtp_pass = os.environ["SMTP_PASS"]

    if not os.path.exists(FEED_FILE):
        print(f"!! {FEED_FILE} 不存在，跳过 Kindle 推送", file=sys.stderr)
        return 0

    with open(FEED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    if not items:
        print("feed_items.json 为空，跳过 Kindle 推送")
        return 0

    book_title = build_epub(items, OUT_EPUB)
    size_kb = os.path.getsize(OUT_EPUB) / 1024
    print(f"✓ 生成 {OUT_EPUB} ({len(items)} 章节, {size_kb:.1f} KB)", flush=True)

    send_via_smtp(
        to_addr=kindle_email,
        subject=book_title,
        epub_path=OUT_EPUB,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
    )
    print(f"✓ Kindle 推送已发送 → {kindle_email}（subject: {book_title}）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
