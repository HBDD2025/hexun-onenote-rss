#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原地规整 OneNote 页面（可指定笔记本/分区范围）。
不重新从源拉，仅对页面里现存 HTML 应用规则。

会做的（对每一页）：
  - 通用文本清理：剥编辑/责编/排版/版权声明/长按二维码 等末尾签名；
    "文章原文"+3行段落规则；头/尾纯图剥离
  - 源特定规则（识别出 hexun / 公众号源时）：
    * 首图替换为 「【本处已删首图】」（慧保天下 + 中国银行保险报 + 今日保 + 保契 + 13个精算师）
    * 中国保险学会：全图替换为 「【此处有图片，但未下载成功】」
    * 中国银行保险报：「来源:」/「来源：」 锚点段及之后全删
    * 保观：「保观 | 聚焦保险创新」 后第 1 图占位
    * 保险一哥：「文章原文」 前最后 1 图占位
  - 兜底：所有剩余 <img> 一律换 「【此处有图片，但未下载成功】」 占位
  - 重新注入字号字体（14pt 宋体）
  - PATCH replace body（不删页，page id 不变）

⚠️ 由于 PATCH API 不支持 multipart 上传图片，原有图片（含成功下载过的）会一律
变成占位文字。这是 OneNote 的硬限制，无法保住图。要保住图就用 refresh_existing.py
（删页 + 重抓源）。

跑法（本地）：

    cd ~/Desktop/hexun-onenote-rss
    git pull

    # 交互式：会先列你所有笔记本/分区，让你输入范围
    python3 tidy_existing.py

    # 直接指定范围
    python3 tidy_existing.py --scope "all"
    python3 tidy_existing.py --scope "公司经营"
    python3 tidy_existing.py --scope "公司经营/重要会议;公司经营/媒体报道"

    # 加 --dry-run 不真改
    python3 tidy_existing.py --scope "公司经营" --dry-run

    # 加 --limit 试水
    python3 tidy_existing.py --scope "all" --limit 3 --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import body_xhtml
import daily_push
import onenote
import rss_lib


BEIJING = timezone(timedelta(hours=8))
SECRETS_PATH = os.path.expanduser("~/hexun-onenote-secrets.json")
STATE_FILE = "tidy_state.json"

URL_IN_BODY_RE = re.compile(
    r'(https?://(?:insurance\.hexun\.com|mp\.weixin\.qq\.com)/[^\s"<>\']+)',
    re.I,
)
BIZ_RE = re.compile(r'__biz=([^&]+)')
ALL_IMG_RE = re.compile(r'<img\b[^>]*/?>', re.I)
# 跟 daily_push.py 保持一致
PLACEHOLDER_FAILED = '<p>【此处有图片，但未下载成功】</p>'
PLACEHOLDER_FIRST_STRIPPED = '<p>【本处已删首图】</p>'


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_secrets():
    if not os.path.exists(SECRETS_PATH):
        print(f"找不到 {SECRETS_PATH}, 先 python3 setup.py")
        sys.exit(1)
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def list_pages(access_token, section_id):
    """列单分区的所有页（保留兼容）"""
    pages = []
    url = (f"{onenote.GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
           f"?$top=100&$select=id,title,createdDateTime")
    while url:
        data = onenote._graph_get(access_token, url)
        pages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return pages


def list_all_pages(access_token, log):
    """列整个 OneNote 账号的所有页（跨所有笔记本所有分区）。
    返回每页含 id/title/createdDateTime/parentSection/parentNotebook（扁平 expand）。"""
    pages = []
    url = (f"{onenote.GRAPH_BASE}/me/onenote/pages"
           f"?$top=100&$expand=parentSection,parentNotebook"
           f"&$select=id,title,createdDateTime")
    fetched = 0
    while url:
        data = onenote._graph_get(access_token, url)
        batch = data.get("value", [])
        pages.extend(batch)
        fetched += len(batch)
        if fetched % 200 == 0 or not data.get("@odata.nextLink"):
            log(f"  …已列 {fetched} 页")
        url = data.get("@odata.nextLink")
    return pages


def get_page_content(access_token, page_id, include_ids=True):
    suffix = "?includeIDs=true" if include_ids else ""
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}/content{suffix}"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def patch_page(access_token, page_id, actions):
    """actions: list of dicts {target, action, content}"""
    req = urllib.request.Request(
        f"{onenote.GRAPH_BASE}/me/onenote/pages/{page_id}/content",
        data=json.dumps(actions).encode("utf-8"),
        method="PATCH",
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH HTTP {e.code}: {body[:400]}")


def extract_orig_url(html):
    m = re.search(r'原文[：:][^<]*?<a[^>]+href="([^"]+)"', html, re.S)
    if m:
        return m.group(1)
    m = URL_IN_BODY_RE.search(html)
    if m:
        return m.group(1)
    return None


def identify_source(url, biz_map):
    if not url:
        return ""
    if "insurance.hexun.com" in url:
        return "和讯"
    bm = BIZ_RE.search(url)
    if bm:
        return biz_map.get(bm.group(1), "")
    return ""


def extract_body_inner(html):
    """提取 <body>...</body> 之间的 HTML 内容。"""
    m = re.search(r'<body[^>]*>(.*?)</body>', html, re.S)
    return m.group(1) if m else None


def apply_image_rules(html, source_label):
    """应用图片规则（绝大多数对所有页面生效，仅 2 条源特定）。

    顺序：
      1. 中国保险学会（源特定）：全图替换占位，结束
      2. 首图：慧保天下走激进（删[开头到首图含]+[首图后第一段]+占位），其他所有页走简单首图替换占位
      3. 通用：「来源:」或「来源：」段落及之后全删
      4. 通用：「保观 | 聚焦保险创新」后第 1 张图替换占位
      5. 正文中其他 <img> 保留原状
    """
    # 1. 中国保险学会：全图占位
    if source_label and "中国保险学会" in source_label:
        return ALL_IMG_RE.sub(PLACEHOLDER_FAILED, html)

    # 2. 首图处理
    if source_label and "慧保天下" in source_label:
        # 激进模式
        m = ALL_IMG_RE.search(html)
        if m:
            after = html[m.end():]
            after = re.sub(
                r'^\s*(?:<br\s*/?>\s*)*<p[^>]*>.*?</p>\s*',
                '', after, count=1, flags=re.S,
            )
            html = PLACEHOLDER_FIRST_STRIPPED + after
    else:
        # 通用首图删（替换为占位）
        m = ALL_IMG_RE.search(html)
        if m:
            p_match = re.search(
                r'<p[^>]*>\s*' + re.escape(html[m.start():m.end()]) + r'\s*</p>',
                html, re.S,
            )
            if p_match:
                html = (html[:p_match.start()] + PLACEHOLDER_FIRST_STRIPPED
                        + html[p_match.end():])
            else:
                html = (html[:m.start()] + PLACEHOLDER_FIRST_STRIPPED
                        + html[m.end():])

    # 3. 通用「来源:」全删
    for anchor in ("来源:", "来源："):
        idx = html.find(anchor)
        if idx < 0:
            continue
        p_start = html.rfind('<p', 0, idx)
        html = html[:p_start] if p_start >= 0 else html[:idx]
        break

    # 4. 通用「保观 | 聚焦保险创新」后第 1 张图替换占位
    anchor = "保观 | 聚焦保险创新"
    idx = html.find(anchor)
    if idx >= 0:
        head = html[:idx + len(anchor)]
        tail = html[idx + len(anchor):]
        tail = ALL_IMG_RE.sub(PLACEHOLDER_FAILED, tail, count=1)
        html = head + tail

    return html


# 老名字兼容
apply_in_place_source_rules = apply_image_rules


# --------- 日期提取与标题生成 ---------

_DATE_PATTERNS = [
    re.compile(r'(20\d{2})-(\d{1,2})-(\d{1,2})\b'),
    re.compile(r'(20\d{2})/(\d{1,2})/(\d{1,2})\b'),
    re.compile(r'(20\d{2})\.(\d{1,2})\.(\d{1,2})\b'),
    re.compile(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日'),
]


def extract_date_from_body(html):
    """从正文 HTML 抽出第一个合理的日期，返回 (yy, mm, dd) 三元组或 None。"""
    # 先剥标签做纯文本搜索，避免匹配到 OneNote 元数据
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            if 2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
                return year, month, day
        except (ValueError, IndexError):
            continue
    return None


def build_new_title(orig_title, body_html, page_created_iso):
    """生成符合 YYMMDD前缀 + 原标题 格式的新标题。
    返回 (new_title, changed_bool)。
    规则：
      - 标题已是 YYMMDD 起头（6 位数字后非数字）→ 保留
      - 标题是 YYYYMMDD 起头（8 位数字后非数字）→ 截前两位（20）
      - 正文找到日期 → 用正文日期
      - 都没有 → 用页创建日期 + （日期为录入日期）后缀
    """
    if not orig_title:
        orig_title = ""

    # 已是 6 位数字开头（后接非数字或字符串结束）
    m6 = re.match(r'^(\d{6})(\D|$)', orig_title)
    if m6:
        # 验证一下是合法的 YYMMDD（年 21-30 之间认为靠谱；防误判其他纯数字开头）
        yy = int(m6.group(1)[:2])
        if 20 <= yy <= 35:
            return orig_title, False

    # 8 位数字开头（YYYYMMDD）→ 截
    m8 = re.match(r'^20(\d{6})(\D|$)', orig_title)
    if m8:
        return m8.group(1) + orig_title[8:], True

    # 从正文找
    body_date = extract_date_from_body(body_html)
    if body_date:
        y, mo, d = body_date
        return f"{y % 100:02d}{mo:02d}{d:02d}{orig_title}", True

    # 用页创建日期
    if page_created_iso:
        try:
            dt = datetime.fromisoformat(page_created_iso.replace("Z", "+00:00"))
            bjt = dt.astimezone(BEIJING)
            return f"{bjt.strftime('%y%m%d')}{orig_title}（日期为录入日期）", True
        except Exception:
            pass
    return orig_title, False


# --------- 用户标注（荧光笔/背景高亮）剥离 ---------

_HIGHLIGHT_STYLE_RE = re.compile(r'background(?:-color)?\s*:\s*[^;"]+;?', re.I)
_ONENOTE_INK_RE = re.compile(r'<img[^>]*\bdata-?renderer[-_]?src=[^>]*?>', re.I)


def strip_annotations(html):
    """剥除用户手动标注（荧光笔/手写）。保守做法：
       - 把 inline style 里的 background / background-color 去掉
       - 删 OneNote 的 ink 渲染图标记 (data-renderer-src 含 ink/inkml)
    保留 <object>（附件）等其他标签。"""
    # 移除 background 样式
    html = _HIGHLIGHT_STYLE_RE.sub('', html)
    # 移除 ink 标记图
    html = _ONENOTE_INK_RE.sub('', html)
    return html


def build_biz_map(log):
    log("构建 __biz → 公众号名 映射...")
    biz_map = {}
    for feed_url in rss_lib.FEEDS:
        try:
            chan_title, items = rss_lib.parse_feed(feed_url)
            for it in items:
                link = it.get("link", "")
                bm = BIZ_RE.search(link)
                if bm:
                    biz_map[bm.group(1)] = chan_title
        except Exception as e:
            log(f"  ! feed 失败：{e}")
    log(f"  映射完成 ({len(biz_map)} 个公众号)")
    return biz_map


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"done_page_ids": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done_page_ids": []}


def save_state(state):
    state["updated_at"] = datetime.now(BEIJING).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tidy_one_page(access_token, page, biz_map, dry_run, log):
    page_id = page["id"]
    title = page.get("title", "")
    sec = (page.get("parentSection") or {}).get("displayName", "?")
    nb = (page.get("parentNotebook") or {}).get("displayName", "?")
    created_iso = page.get("createdDateTime", "")
    log(f"→ [{nb} / {sec}] {title[:50]}")

    try:
        html = get_page_content(access_token, page_id, include_ids=False)
    except Exception as e:
        log(f"  ! GET 失败：{e}")
        return "fail"

    orig_url = extract_orig_url(html)
    source_label = identify_source(orig_url, biz_map)

    body_inner = extract_body_inner(html)
    if not body_inner:
        log(f"  ! 没找到 <body>，跳过")
        return "skip"

    # === 应用规则 ===
    # 1. 通用文本清理（编辑/版权声明锚点、文章原文+3行、头尾纯图）
    new_body = body_xhtml._strip_promo(body_inner)
    # 2. 图片规则（首图/中国保险学会全图/慧保天下激进/来源:/保观锚点）
    new_body = apply_image_rules(new_body, source_label)
    # 3. 剥用户标注（荧光笔背景、手写墨迹）
    new_body = strip_annotations(new_body)
    # 4. 字体字号统一
    element_style = (
        f"font-family:'{onenote.PAGE_FONT_FAMILY}';"
        f"font-size:{onenote.PAGE_FONT_SIZE_PT}.0pt"
    )
    new_body = onenote._inject_inline_style(new_body, element_style)
    new_body = onenote._wrap_text_in_span(new_body, element_style)

    # === 新标题 ===
    new_title, title_changed = build_new_title(title, new_body, created_iso)
    body_changed = new_body.strip() != body_inner.strip()

    if not body_changed and not title_changed:
        log(f"  · 标题和正文都无变化，跳过")
        return "skip"

    log(f"  源:{source_label or '(未知)'}{' / 标题→ ' + new_title[:40] if title_changed else ''}")
    if dry_run:
        log(f"  [dry-run] body {len(body_inner)} → {len(new_body)} 字"
            f"{' / 标题改' if title_changed else ''}")
        return "ok"

    actions = []
    if title_changed:
        actions.append({
            "target": "title",
            "action": "replace",
            "content": f"<title>{new_title}</title>",
        })
    if body_changed:
        actions.append({
            "target": "body",
            "action": "replace",
            "content": new_body,
        })
    try:
        patch_page(access_token, page_id, actions)
        log(f"  ✓ 已更新")
    except Exception as e:
        log(f"  ! PATCH 失败：{e}")
        return "fail"
    return "ok"


# --------- 范围（笔记本/分区）选择 ---------

def list_notebooks_with_sections(access_token):
    """列出所有笔记本及其分区。"""
    url = (f"{onenote.GRAPH_BASE}/me/onenote/notebooks"
           f"?$expand=sections($select=id,displayName)&$select=id,displayName")
    notebooks = []
    while url:
        data = onenote._graph_get(access_token, url)
        notebooks.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return notebooks


def print_notebooks_tree(notebooks):
    print("\n=== 你的 OneNote 笔记本 / 分区 ===")
    for nb in notebooks:
        nb_name = nb.get("displayName", "?")
        print(f"  📓 {nb_name}")
        for sec in nb.get("sections", []):
            print(f"     - {sec.get('displayName', '?')}")
    print("=" * 40)


def parse_scope(spec_str):
    """'all' → None; '公司经营/会议;Chen/RSS syn2' → [('公司经营','会议'),('Chen','RSS syn2')]
       '公司经营' → [('公司经营', None)]  整个笔记本"""
    if not spec_str:
        return None
    s = spec_str.strip()
    if s.lower() in ("all", "全部", "*"):
        return None
    scopes = []
    for token in s.split(";"):
        token = token.strip()
        if not token:
            continue
        parts = token.split("/", 1)
        nb = parts[0].strip()
        sec = parts[1].strip() if len(parts) > 1 else None
        scopes.append((nb, sec))
    return scopes


def validate_scope(scopes, notebooks):
    """校验 scopes 里的每条都对得上现有 notebook/section。返回 (ok, error_msgs)."""
    nb_names = {nb.get("displayName") for nb in notebooks}
    sec_by_nb = {
        nb.get("displayName"): {s.get("displayName") for s in nb.get("sections", [])}
        for nb in notebooks
    }
    errors = []
    for nb, sec in scopes:
        if nb not in nb_names:
            errors.append(f"笔记本不存在：{nb}")
        elif sec is not None and sec not in sec_by_nb.get(nb, set()):
            errors.append(f"分区不存在：{nb}/{sec}")
    return (not errors), errors


def page_in_scope(page, scopes):
    if scopes is None:
        return True
    sec_info = page.get("parentSection") or {}
    nb_info = page.get("parentNotebook") or {}
    sec_name = sec_info.get("displayName", "")
    nb_name = nb_info.get("displayName", "")
    for nb_spec, sec_spec in scopes:
        if nb_name == nb_spec:
            if sec_spec is None or sec_name == sec_spec:
                return True
    return False


def prompt_for_scope(notebooks):
    """显示树 + 提示用户输入范围。"""
    print_notebooks_tree(notebooks)
    print()
    print("输入要处理的范围：")
    print("  - 'all' = 全部笔记本所有分区")
    print("  - '公司经营' = 整个笔记本（所有分区）")
    print("  - '公司经营/重要会议' = 指定分区")
    print("  - 多个用分号 ';' 分隔，比如 '公司经营/重要会议;公司经营/媒体报道'")
    while True:
        raw = input("\n范围 > ").strip()
        scopes = parse_scope(raw)
        if scopes is None:
            print("已选：全部")
            return None
        ok, errors = validate_scope(scopes, notebooks)
        if ok:
            print("已选范围：")
            for nb, sec in scopes:
                print(f"  - {nb}{'/' + sec if sec else ' (整个笔记本)'}")
            return scopes
        for e in errors:
            print(f"  ⚠ {e}")
        print("请重新输入。")


def main():
    ap = argparse.ArgumentParser(description="原地规整 OneNote 页面")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--scope", default=None,
                    help='范围。"all" / "公司经营" / "公司经营/重要会议" / 多个分号分隔。'
                         '不传时进入交互式选择')
    ap.add_argument("--title-kw", default="", help="只处理标题含该关键字的页")
    ap.add_argument("--since", default="", help="只处理 YYMMDD 前缀 >= 该日期的页 (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print("OneNote 原地规整")
    print("=" * 64)

    secrets = load_secrets()
    log("刷新 access_token...")
    access_token, new_refresh = onenote.refresh_access_token(
        secrets["AZURE_CLIENT_ID"], secrets["MS_REFRESH_TOKEN"]
    )
    if new_refresh != secrets["MS_REFRESH_TOKEN"]:
        secrets["MS_REFRESH_TOKEN"] = new_refresh
        with open(SECRETS_PATH, "w", encoding="utf-8") as f:
            json.dump(secrets, f, indent=2, ensure_ascii=False)
        log("refresh_token 已更新本地副本")

    # 列笔记本/分区 + 让用户选范围
    log("\n列出你 OneNote 所有笔记本和分区...")
    notebooks = list_notebooks_with_sections(access_token)

    if args.scope is None:
        scopes = prompt_for_scope(notebooks)
    else:
        scopes = parse_scope(args.scope)
        if scopes is not None:
            ok, errors = validate_scope(scopes, notebooks)
            if not ok:
                for e in errors:
                    print(f"  ⚠ {e}")
                sys.exit(1)
            log("已选范围：")
            for nb, sec in scopes:
                log(f"  - {nb}{'/' + sec if sec else ' (整个笔记本)'}")
        else:
            log("已选：全部")

    log("\n列所有页面...")
    pages = list_all_pages(access_token, log)
    log(f"  共 {len(pages)} 页")

    # 范围过滤
    before = len(pages)
    pages = [p for p in pages if page_in_scope(p, scopes)]
    log(f"  按范围过滤：{before} → {len(pages)}")

    if args.title_kw:
        before = len(pages)
        pages = [p for p in pages if args.title_kw in p.get("title", "")]
        log(f"  按 title 过滤：{before} → {len(pages)}")
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").date()
            since_prefix = since_dt.strftime("%y%m%d")
            before = len(pages)
            pages = [p for p in pages if (p.get("title", "")[:6] or "000000") >= since_prefix]
            log(f"  按 since 过滤：{before} → {len(pages)}")
        except ValueError:
            print(f"--since 日期格式错")
            sys.exit(1)

    state = load_state()
    done = set(state.get("done_page_ids", []))
    pending = [p for p in pages if p["id"] not in done]
    log(f"  已 tidy 过 {len(done)}，待处理 {len(pending)}")

    if args.limit > 0:
        pending = pending[:args.limit]
        log(f"  --limit 截取后：{len(pending)}")

    if not pending:
        log("没有待处理页面。")
        return

    if not args.dry_run:
        est_min = len(pending) * 4 / 60
        log(f"\n预计耗时 ~{est_min:.0f} 分钟（按 4 秒/页，比 refresh_existing 快得多）")
        ans = input("继续？(Y/n)：").strip().lower()
        if ans == "n":
            return

    biz_map = build_biz_map(log) if not args.dry_run else {}

    n_ok = n_skip = n_fail = 0
    for i, page in enumerate(pending, 1):
        log(f"\n[{i}/{len(pending)}]")
        try:
            result = tidy_one_page(access_token, page, biz_map, args.dry_run, log)
        except Exception:
            log(f"  ! 异常：{traceback.format_exc().splitlines()[-1]}")
            result = "fail"
        if result == "ok":
            n_ok += 1
            if not args.dry_run:
                done.add(page["id"])
        elif result == "skip":
            n_skip += 1
            done.add(page["id"])
        else:
            n_fail += 1

        if (i % 20) == 0 and not args.dry_run:
            state["done_page_ids"] = sorted(done)
            save_state(state)
            log(f"  ✓ 进度保存（OK {n_ok} / 跳过 {n_skip} / 失败 {n_fail}）")
        time.sleep(0.5)

    if not args.dry_run:
        state["done_page_ids"] = sorted(done)
        save_state(state)
    log("\n" + "=" * 64)
    log(f"完成。原地更新 {n_ok}，跳过 {n_skip}，失败 {n_fail}")
    log("=" * 64)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断（每 20 页保存一次进度，再跑会接着处理剩下的）")
