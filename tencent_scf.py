# -*- coding: utf-8 -*-
"""
腾讯云 SCF（云函数）反向代理：把 hexun.com 请求中转一下。

为什么走 SCF 而不是 CF Worker / 付费代理：
- SCF 出口 IP 是腾讯云自家网段，腾讯 EdgeOne（也是腾讯产品）不可能拦自家平台
- 免费额度：100 万次调用/月 + 40 万 GB·秒/月，我们用量 1.4 万次/月、9K GB·秒/月
- 实际花费：仅公网出流量 ~50MB/月 × ¥0.8/GB ≈ ¥0.04/月

部署：
  1. 腾讯云控制台 → 云函数 SCF → 新建函数 → 自定义创建 → 事件函数
  2. 运行环境选 Python 3.10（或 3.9）
  3. 把本文件代码整段粘到 index.py（默认入口文件）
  4. 函数配置 → 执行超时时间改成 30 秒（默认 3 秒不够）
  5. 触发管理 → 新建触发器 → API 网关触发，免鉴权
  6. 部署完会给出 https://xxxxx.gz.apigw.tencentcs.com/release/ 这种地址
  7. 把它 + ?url={url} 配进 GH Secret HEXUN_PROXY_URL：
       https://xxxxx.gz.apigw.tencentcs.com/release/?url={url}
"""

import base64
import re
import urllib.error
import urllib.parse
import urllib.request


# 白名单：只允许 *.hexun.com，防 SCF URL 泄露后被滥用做开放代理
_ALLOWED_HOST_RX = re.compile(r"(^|\.)hexun\.com$", re.I)

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _resp(status, body, headers=None):
    """返回 API 网关响应。body 可以是 str 或 bytes；bytes 自动 base64。"""
    out_headers = {"content-type": "text/plain; charset=utf-8"}
    if headers:
        for k, v in headers.items():
            out_headers[k.lower()] = v
    if isinstance(body, bytes):
        return {
            "statusCode": status,
            "headers": out_headers,
            "body": base64.b64encode(body).decode("ascii"),
            "isBase64Encoded": True,
        }
    return {
        "statusCode": status,
        "headers": out_headers,
        "body": body if isinstance(body, str) else str(body),
        "isBase64Encoded": False,
    }


def _extract_target_url(event):
    """从 API 网关事件里取 ?url=... 参数。兼容多种事件结构。"""
    qs = event.get("queryString") or event.get("queryStringParameters") or {}
    if isinstance(qs, dict):
        return qs.get("url")
    if isinstance(qs, str):
        parsed = urllib.parse.parse_qs(qs)
        vals = parsed.get("url")
        return vals[0] if vals else None
    return None


def main_handler(event, context):
    target = _extract_target_url(event)
    if not target:
        return _resp(400, "usage: /?url=https%3A%2F%2Finsurance.hexun.com%2F...")

    # 域名白名单
    try:
        parsed = urllib.parse.urlparse(target)
    except Exception as e:
        return _resp(400, f"invalid url: {e}")
    host = parsed.hostname or ""
    if not _ALLOWED_HOST_RX.search(host):
        return _resp(403, f"only *.hexun.com allowed, got: {host}")

    # 透传客户端 headers，剔掉 host/x-forwarded-* 等冲突项 + 网关自带的内部 header
    in_headers = event.get("headers") or {}
    upstream_headers = {}
    for k, v in in_headers.items():
        lk = k.lower()
        if lk in ("host", "content-length", "x-forwarded-for", "x-forwarded-proto",
                  "x-real-ip", "x-api-requestid", "x-anonymous-consumer"):
            continue
        if lk.startswith("x-apigw-") or lk.startswith("x-scf-") or lk.startswith("x-tc-"):
            continue
        upstream_headers[lk] = v
    # 必填头补默认
    upstream_headers.setdefault("user-agent", _DEFAULT_UA)
    upstream_headers.setdefault("accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    upstream_headers.setdefault("accept-language", "zh-CN,zh;q=0.9,en;q=0.8")
    upstream_headers.setdefault("accept-encoding", "gzip, deflate")
    upstream_headers.setdefault("referer", "https://insurance.hexun.com/")

    method = (event.get("httpMethod") or "GET").upper()
    req_body = None
    if method in ("POST", "PUT", "PATCH"):
        raw_body = event.get("body")
        if event.get("isBase64Encoded") and raw_body:
            req_body = base64.b64decode(raw_body)
        elif raw_body:
            req_body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body

    req = urllib.request.Request(target, data=req_body, method=method)
    for k, v in upstream_headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            up_headers = {}
            for k, v in r.headers.items():
                lk = k.lower()
                if lk in ("transfer-encoding", "connection", "set-cookie",
                          "content-length"):  # content-length 让网关自己算
                    continue
                up_headers[lk] = v
            up_headers["x-proxied-by"] = "tencent-scf-hexun"
            return _resp(r.status, raw, headers=up_headers)
    except urllib.error.HTTPError as e:
        # 上游业务错误：把 4xx/5xx 状态和 body 透传，方便诊断
        try:
            body = e.read()
        except Exception:
            body = b""
        h = dict(e.headers.items()) if e.headers else {}
        for bad in ("Transfer-Encoding", "transfer-encoding",
                    "Connection", "connection",
                    "Set-Cookie", "set-cookie",
                    "Content-Length", "content-length"):
            h.pop(bad, None)
        h["x-proxied-by"] = "tencent-scf-hexun"
        return _resp(e.code, body, headers=h)
    except Exception as e:
        return _resp(502, f"upstream error: {type(e).__name__}: {e}")
