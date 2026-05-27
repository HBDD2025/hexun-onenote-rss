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
            # 微信公众号用 data-src 做懒加载（src 是 1x1 占位），优先取 data-src
            src = ""
            real_src = ""
            for k, v in attrs:
                kl = k.lower()
                if kl == "data-src" and v:
                    real_src = v
                elif kl == "src" and v:
                    src = v
            chosen = real_src or src
            if not chosen:
                return
            abs_src = urljoin(self.base_url, chosen)
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
        # 关键：WeChat 源（尤其中国银行保险报）爱用 <p><br/></p> 或 <p>&nbsp;</p>
        # 当段落分隔，这种"伪空段落"在 OneNote 里会占整整一行。必须连同内嵌的
        # 内联格式标签一起识别并清除，否则段距永远是 2 行。
        # 注意：图段（<p><img/></p>）和 hr 必须保留。
        empty_p_rx = re.compile(
            r'<p[^>]*>'
            r'(?:\s|\xa0|&nbsp;'
            r'|<br\s*/?>'
            r'|<span[^>]*>|</span>'
            r'|<b>|</b>|<i>|</i>|<em[^>]*>|</em>|<strong[^>]*>|</strong>'
            r'|<u>|</u>|<sup[^>]*>|</sup>|<sub[^>]*>|</sub>'
            r')*</p>',
            re.I,
        )
        # 多轮：嵌套清空后可能暴露新的空段
        for _ in range(4):
            new_out = empty_p_rx.sub("", out)
            if new_out == out:
                break
            out = new_out
        return out.strip()


def _text_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr_escape(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _strip_promo(xhtml):
    """
    剥掉广告条幅 / 公众号末尾推广区。
    - 尾：找"编辑/责编/排版/校对/审核/版权声明/长按识别二维码"等签名锚点，剪掉之后所有内容
    - 尾：再去掉末尾连续的纯图段落
    - 头：剥掉首张 banner（如果首图出现在第一个含文字段落之前）
    """
    if not xhtml:
        return xhtml

    # 末尾签名锚点。覆盖多种公众号常见的写法
    # 关键：尾部允许多个嵌套关闭标签（如 </span></p>），因为 WeChat 经常 <p><span>编辑 X</span></p>
    _TAIL = r'(?:\s*</[^>]+>)*\s*</p>'
    end_anchors = [
        # 整段 <p>...《XXX》编辑 张三</p>
        re.compile(r'<p[^>]*>\s*(?:<[^>]+>\s*)*《[^》]+》\s*编辑\b[^<]{0,40}' + _TAIL, re.I),
        # 整段 <p>...(本期/责任/责)编辑 [：:/／\s]\s*X</p>
        re.compile(r'<p[^>]*>\s*(?:<[^>]+>\s*)*(?:本期|责任|责)?编辑\s*[:：/／\s][^<]{1,40}' + _TAIL, re.I),
        # 整段 <p>...排版/校对/审核/审定/审签/主编/监制 [：:/／]\s*X</p>
        re.compile(r'<p[^>]*>\s*(?:<[^>]+>\s*)*(?:排版|校对|审核|审定|审签|主编|监制|来源)\s*[:：/／][^<]{1,60}' + _TAIL, re.I),
        # 【版权声明】整段
        re.compile(r'<p[^>]*>[^<]*【版权声明】.*?</p>', re.I | re.S),
        # 长按识别二维码
        re.compile(r'<p[^>]*>[^<]*长按识别二维码.*?</p>', re.I | re.S),
        # 兜底：裸 "《XXX》编辑 X"（不在 <p> 包裹中）
        re.compile(r'《[^》]+》\s*编辑\b[^<]{1,40}', re.I),
        # 兜底：裸 "【版权声明】" 文本
        re.compile(r'【版权声明】', re.I),
    ]

    # 取所有匹配中位置最早的（最早出现 = 之后的全是垃圾）
    cut_at = None
    for pat in end_anchors:
        for m in pat.finditer(xhtml):
            # 如果匹配整段（以 </p> 结尾）就用 m.end()，否则找下一个 </p>
            if m.group(0).endswith("</p>"):
                pos = m.end()
            else:
                close_p = xhtml.find("</p>", m.end())
                pos = close_p + 4 if close_p != -1 else m.end()
            if cut_at is None or pos < cut_at:
                cut_at = pos
    if cut_at is not None:
        xhtml = xhtml[:cut_at]

    # 1b. 通用规则：如含"文章原文"，倒推找最后一段 ≥40 字（够 3 行）的 <p>，砍掉之后所有
    if "文章原文" in xhtml:
        end_pos = xhtml.find("文章原文")
        paragraphs = list(re.finditer(r'<p[^>]*>(.*?)</p>', xhtml[:end_pos], re.S))
        last_cut = None
        for m in reversed(paragraphs):
            inner = m.group(1)
            text = re.sub(r'<[^>]+>', '', inner)
            text = re.sub(r'\s+', '', text)
            if len(text) >= 40:
                last_cut = m.end()
                break
        if last_cut is not None:
            xhtml = xhtml[:last_cut]
        else:
            xhtml = xhtml[:end_pos]

    # 2. 尾部：去掉残余的纯图/空段落
    while True:
        m = re.search(
            r'(?:<img[^>]*/>|<p[^>]*>(?:\s*<img[^>]*/>\s*|\s*)+</p>)\s*$',
            xhtml,
        )
        if m and not re.search(r'[一-鿿]', xhtml[m.start():]):
            xhtml = xhtml[:m.start()]
        else:
            break

    # 3. 头部：找第一个含汉字的 <p>，如果它前面只有 <img>/空段，就剥掉
    m = re.search(r'<p[^>]*>[^<]*[一-鿿]', xhtml)
    if m:
        leading = xhtml[:m.start()]
        # 留下的若只是图、空段、br，剥掉
        residue = re.sub(r'<img[^>]*/>', '', leading)
        residue = re.sub(r'<p[^>]*>\s*</p>', '', residue)
        residue = re.sub(r'<br\s*/>', '', residue)
        if not residue.strip():
            xhtml = xhtml[m.start():]

    return xhtml.strip()


def convert(body_html, base_url=""):
    """
    输入：从 art_contextBox / WeChat js_content 取出的原始 HTML 片段
    输出：(xhtml_string, [img_url, ...])
    """
    if not body_html:
        return "", []
    # 先去掉末尾样板（文本级 disclaimer）
    for pat in DISCLAIMER_PATTERNS:
        body_html = pat.sub("", body_html)
    builder = XhtmlBuilder(base_url=base_url)
    builder.feed(body_html)
    xhtml = builder.get_html()
    xhtml = _strip_promo(xhtml)
    return xhtml, builder.images
