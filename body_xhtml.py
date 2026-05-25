# -*- coding: utf-8 -*-
"""
把和讯文章正文 HTML 转换成 OneNote 友好的 XHTML：
- 仅保留段落结构、加粗/斜体/链接/列表/表格/图片
- 剥掉 class / style / 内嵌脚本
- 图片 src 替换为 name:imgN，配合 multipart 上传二进制
- 末尾的"责任编辑"/"免责声明"自动剪掉
"""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin


# OneNote 接受的精简标签集（div 故意排除：它只是布局壳，剥掉防止结构歪掉）
ALLOWED_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                 "ul", "ol", "li", "blockquote", "table", "tr", "td", "th", "thead", "tbody"}
ALLOWED_INLINE = {"b", "strong", "em", "i", "u", "span", "br", "sup", "sub"}  # 'a' 故意剔除：去掉所有蓝色超链接
ALLOWED_VOID = {"br", "img", "hr"}
ALLOWED_ALL = ALLOWED_BLOCK | ALLOWED_INLINE | {"img", "hr"}

SKIP_TAGS = {"script", "style", "noscript", "iframe", "object", "embed", "form", "input", "button"}

# 末尾样板剪除（覆盖 <p>/<div> 包裹与裸文，从第一个责任编辑/免责声明出现处之后全部砍掉）
DISCLAIMER_PATTERNS = [
    re.compile(r"<(?:p|div)[^>]*>\s*[（(]\s*责任编辑.*", re.I | re.S),
    re.compile(r"<(?:p|div)[^>]*>\s*【免责声明】.*", re.I | re.S),
    re.compile(r"[（(]\s*责任编辑[:：].*", re.S),
    re.compile(r"【免责声明】.*", re.S),
]


class XhtmlBuilder(HTMLParser):
    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts = []
        self.skip_depth = 0
        self.images = []  # list of resolved absolute image URLs in DOM order

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "img":
            src = ""
            for k, v in attrs:
                if k.lower() == "src" and v:
                    src = v
                    break
            if not src:
                return
            abs_src = urljoin(self.base_url, src)
            idx = len(self.images)
            self.images.append(abs_src)
            # XHTML self-closing
            self.parts.append(f'<img src="name:img{idx}" />')
            return
        if tag == "a":
            # 故意不写开始标签，文本会被 handle_data 直接保留
            return
        if tag in ALLOWED_ALL:
            if tag in ALLOWED_VOID:
                self.parts.append(f"<{tag} />")
            else:
                self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        # 处理 <img/>、<br/> 自闭合
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "img" or tag in ALLOWED_VOID:
            return  # 自闭合，无需结束标签
        if tag in ALLOWED_ALL:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if not data:
            return
        # 把多个空白压成一个空格（不动换行因为会被块级标签消化）
        cleaned = []
        for ch in data:
            if ch in ("\n", "\r"):
                cleaned.append(" ")
            elif ch.isspace():
                cleaned.append(" ")
            else:
                cleaned.append(ch)
        s = re.sub(r" +", " ", "".join(cleaned))
        self.parts.append(_text_escape(s))

    def get_html(self):
        out = "".join(self.parts)
        # 清掉 OneNote 显示中讨厌的空段
        out = re.sub(r"<p>\s*</p>", "", out)
        out = re.sub(r"<div>\s*</div>", "", out)
        return out.strip()


def _text_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr_escape(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def convert(body_html, base_url=""):
    """
    输入：从 art_contextBox 取出的原始 HTML 片段
    输出：(xhtml_string, [img_url, ...])
    """
    if not body_html:
        return "", []
    # 先去掉末尾样板
    for pat in DISCLAIMER_PATTERNS:
        body_html = pat.sub("", body_html)
    builder = XhtmlBuilder(base_url=base_url)
    builder.feed(body_html)
    return builder.get_html(), builder.images
