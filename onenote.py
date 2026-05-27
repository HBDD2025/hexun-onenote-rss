# -*- coding: utf-8 -*-
"""OneNote Graph API 客户端（个人微软账号 + 设备码授权 + 分区写入）。"""

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


AUTH_BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = "Notes.ReadWrite offline_access"


# ---------------------- OAuth：设备码流程（首次） ----------------------

def device_code_start(client_id):
    """返回 {device_code, user_code, verification_uri, expires_in, interval, ...}"""
    data = urllib.parse.urlencode({"client_id": client_id, "scope": SCOPES}).encode()
    req = urllib.request.Request(f"{AUTH_BASE}/devicecode", data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def device_code_poll(client_id, device_code, interval=5, expires_in=900):
    """轮询直到用户授权完成。返回 token dict（含 refresh_token）。"""
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        }).encode()
        req = urllib.request.Request(f"{AUTH_BASE}/token", data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("authorization_declined", "expired_token", "bad_verification_code"):
                raise RuntimeError(f"授权失败：{err} - {body.get('error_description', '')}")
            raise RuntimeError(f"token endpoint error: {body}")
    raise RuntimeError("device code 已过期，请重新发起")


# ---------------------- OAuth：刷新 access_token ----------------------

def refresh_access_token(client_id, refresh_token):
    """用 refresh_token 换新的 access_token。返回 (access_token, new_refresh_token)。"""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": SCOPES,
    }).encode()
    req = urllib.request.Request(f"{AUTH_BASE}/token", data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            t = json.loads(r.read())
            return t["access_token"], t.get("refresh_token", refresh_token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"refresh_token 失败：{e.code} {body}")


# ---------------------- Graph：分区/页面 ----------------------

def _graph_get(access_token, path):
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def list_sections(access_token):
    """列出当前用户所有 OneNote 分区。返回 [{id, displayName, notebookName}, ...]"""
    out = []
    url = f"{GRAPH_BASE}/me/onenote/sections?$expand=parentNotebook&$top=100"
    while url:
        data = _graph_get(access_token, url)
        for s in data.get("value", []):
            out.append({
                "id": s["id"],
                "displayName": s.get("displayName", "(未命名)"),
                "notebookName": (s.get("parentNotebook") or {}).get("displayName", "(未知笔记本)"),
            })
        url = data.get("@odata.nextLink")
    return out


# ---------------------- Graph：发布页面（multipart） ----------------------

# ---- 页面样式（所有新页面统一）----
# OneNote 的 CSS 比较挑食，格式按官方文档严格写：冒号后空格、字号带 .0pt、字体名带中文
PAGE_FONT_FAMILY = "宋体"                      # 中文名比英文 SimSun 在 OneNote 各端识别率高
PAGE_FONT_SIZE_PT = 14                         # 14 pt
PAGE_OUTLINE_WIDTH_PX = 1125
PAGE_OUTLINE_TOP_PX = 240

import re as _re_for_style
_BLOCK_TAGS_FOR_STYLE = ("p", "h1", "h2", "h3", "h4", "h5", "h6",
                          "li", "td", "th", "blockquote", "span")
_BLOCK_TAG_RX = _re_for_style.compile(
    r"<(" + "|".join(_BLOCK_TAGS_FOR_STYLE) + r")((?:\s[^>]*)?)>",
    _re_for_style.IGNORECASE,
)


def _inject_inline_style(html, style):
    """给所有块级元素加 inline style，**追加到 style 末尾**让我们的样式优先生效。
    （CSS 同属性后写覆盖先写，所以追加确保 margin 等被强制为我们的值。）"""
    style_attr_rx = _re_for_style.compile(r'style="([^"]*)"')
    def _add(m):
        tag = m.group(1)
        attrs = m.group(2) or ""
        sm = style_attr_rx.search(attrs)
        if sm:
            existing = sm.group(1).strip().rstrip(';')
            sep = ';' if existing else ''
            new_attrs = (attrs[:sm.start()]
                         + f'style="{existing}{sep}{style}"'
                         + attrs[sm.end():])
            return f"<{tag}{new_attrs}>"
        return f"<{tag}{attrs} style=\"{style}\">"
    return _BLOCK_TAG_RX.sub(_add, html)


def _wrap_text_in_span(html, style):
    """把每个 <p>... 直接文本内容</p> 用 <span style="..."> 包起来。
    OneNote 经常忽略 <p> 上的字号字体，但 <span> 内联 style 一般认。"""
    def _wrap(m):
        opening = m.group(1)
        inner = m.group(2)
        # 如果内部已经全是 <span>/<b>/<a> 等，简单包一层
        return f'{opening}<span style="{style}">{inner}</span></p>'
    # 仅匹配那些 <p>...</p> 不含嵌套 <p> 的（最普遍情况）
    return _re_for_style.sub(
        r'(<p(?:\s[^>]*)?>)((?:(?!</p>).)*)</p>',
        _wrap, html, flags=_re_for_style.DOTALL,
    )


def create_page(access_token, section_id, title, xhtml_body, images, created_iso=None):
    """
    把一篇带图片的 OneNote 页面发布到指定分区。
    - xhtml_body: <body>...</body> 之间的内容（HTML 片段）
    - images: [(bytes, content_type), ...] 对应 xhtml 中 name:img0, name:img1, ...
    - created_iso: 设置页面 created 元数据（ISO8601 + 时区）

    所有正文统一包到一个加宽的 outline div 里，应用 14pt 宋体；OneNote 会按这个
    width 渲染（默认约 600px，这里加宽到 900px，每行字数提升 ~25-50%）。
    """
    boundary = "----OneNoteBoundary" + secrets.token_hex(16)
    crlf = b"\r\n"

    # Presentation 部分（XHTML 全页）
    meta_created = f'<meta name="created" content="{created_iso}"/>' if created_iso else ""
    # CSS 不带空格（OneNote 严格点）；字体名带单引号；字号 14.0pt
    # margin-top/bottom 压紧段落间距（OneNote 默认 5.5pt + 5.5pt，看起来空两行；压成 2pt 约半行）
    element_style = (
        f"font-family:'{PAGE_FONT_FAMILY}';font-size:{PAGE_FONT_SIZE_PT}.0pt;"
        f"margin-top:0pt;margin-bottom:2pt"
    )
    # outline 严格只放 position/left/top/width（OneNote 明确文档要求；混入 font 会被整段 strip）
    outline_style = (
        f"position:absolute;left:48px;"
        f"top:{PAGE_OUTLINE_TOP_PX}px;"
        f"width:{PAGE_OUTLINE_WIDTH_PX}px;"
    )
    # 给每个 <p>/<h>/<li> 加 inline style，再用 <span> 包内文（双重保险）
    styled_body = _inject_inline_style(xhtml_body, element_style)
    styled_body = _wrap_text_in_span(styled_body, element_style)
    presentation_html = (
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head>\n'
        f'  <title>{_x_escape(title)}</title>\n'
        f'  {meta_created}\n'
        '</head>\n'
        '<body>\n'
        f'<div data-id="onp-outline" style="{outline_style}">\n'
        f'{styled_body}\n'
        '</div>\n'
        '</body>\n'
        '</html>\n'
    ).encode("utf-8")

    body = bytearray()

    def add_part(name, content_type, payload):
        body.extend(f"--{boundary}".encode())
        body.extend(crlf)
        body.extend(f'Content-Disposition: form-data; name="{name}"'.encode())
        body.extend(crlf)
        body.extend(f"Content-Type: {content_type}".encode())
        body.extend(crlf)
        body.extend(crlf)
        body.extend(payload)
        body.extend(crlf)

    add_part("Presentation", "application/xhtml+xml", presentation_html)
    for i, (img_bytes, img_ctype) in enumerate(images):
        add_part(f"img{i}", img_ctype or "application/octet-stream", img_bytes)
    body.extend(f"--{boundary}--".encode())
    body.extend(crlf)

    url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
    req = urllib.request.Request(url, data=bytes(body), method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"create_page 失败 HTTP {e.code}: {body_text[:800]}")


def _x_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
