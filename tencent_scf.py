# -*- coding: utf-8 -*-
"""
腾讯云 SCF Web 函数：hexun.com 反向代理。

为什么走 SCF：
- SCF 出口 IP 是腾讯云自家网段，腾讯 EdgeOne（也是腾讯产品）不会拦自家平台
- 免费额度：100 万次调用/月 + 40 万 GB·秒/月。我们用 1.4 万次/月，几乎全免费
- 月费仅公网出流量 ~50MB × ¥0.8/GB ≈ ¥0.04

⚠️ 腾讯云 2025-06-30 已下线 API 网关触发器，必须用「函数 URL」（Function URL）。
所以这是 Web 函数版本，不是事件函数版本。

部署：
  1. 腾讯云控制台 → 云函数 SCF → 函数服务 → 新建
  2. 自定义创建 → 函数类型选「Web 函数」→ 运行环境 Python 3.10
  3. 创建方式选「使用模板」→ 找 "Python Flask Hello World" 或类似带 scf_bootstrap 的模板
     （为的是拿到一个能直接跑起来的 scf_bootstrap 启动脚本，省得自己写）
  4. 创建好后，进 函数代码 → 把 index.py 内容**全部替换**成本文件代码
  5. scf_bootstrap 文件保留不动
  6. 函数配置 → 执行超时时间改成 30 秒（默认 3 秒不够）
  7. 触发管理 → 创建触发器 → 选「函数 URL」类型 → 鉴权方式「免鉴权」
  8. 部署完拿到一个 https://service-xxxxxxxx-xxxxxxx.xx.tencentscf.com/ 之类的 URL
  9. 浏览器测：https://你的SCFURL/?url=https%3A%2F%2Finsurance.hexun.com%2Fbxhyzx%2Findex.html
  10. 通了的话，把 URL + ?url={url} 配进 GH Secret HEXUN_PROXY_URL：
        https://service-xxx.xxx.tencentscf.com/?url={url}

本代码使用纯 stdlib（无 Flask 依赖），跟 scf_bootstrap 里那行
`python3 index.py` 完全兼容。
"""

import http.server
import os
import re
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request


# 只放行 *.hexun.com，防止 SCF URL 泄露后被滥用做开放代理
_ALLOWED_HOST_RX = re.compile(r"(^|\.)hexun\.com$", re.I)

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "scf-hexun-proxy/1.0"

    def do_GET(self):    self._handle()
    def do_HEAD(self):   self._handle()
    def do_POST(self):   self._handle()
    def do_PUT(self):    self._handle()
    def do_DELETE(self): self._handle()

    def log_message(self, fmt, *args):
        sys.stderr.write("[scf] " + (fmt % args) + "\n")

    def _handle(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            target = (qs.get("url") or [None])[0]
            if not target:
                return self._reply(400,
                    b"usage: /?url=https%3A%2F%2Finsurance.hexun.com%2F...",
                    "text/plain; charset=utf-8")

            try:
                t_parsed = urllib.parse.urlparse(target)
            except Exception as e:
                return self._reply(400, f"invalid url: {e}".encode(),
                                   "text/plain; charset=utf-8")
            host = t_parsed.hostname or ""
            if not _ALLOWED_HOST_RX.search(host):
                return self._reply(403,
                    f"only *.hexun.com allowed, got: {host}".encode(),
                    "text/plain; charset=utf-8")

            # 客户端 → 上游 header 透传，剔掉冲突/内部头
            upstream_headers = {}
            for k in list(self.headers.keys()):
                lk = k.lower()
                if lk in ("host", "content-length", "x-forwarded-for",
                          "x-forwarded-proto", "x-real-ip",
                          "x-api-requestid", "x-anonymous-consumer", "expect"):
                    continue
                if lk.startswith("x-apigw-") or lk.startswith("x-scf-") \
                        or lk.startswith("x-tc-"):
                    continue
                upstream_headers[lk] = self.headers[k]
            upstream_headers.setdefault("user-agent", _DEFAULT_UA)
            upstream_headers.setdefault("accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            upstream_headers.setdefault("accept-language",
                "zh-CN,zh;q=0.9,en;q=0.8")
            upstream_headers.setdefault("accept-encoding", "gzip, deflate")
            upstream_headers.setdefault("referer", "https://insurance.hexun.com/")

            # 透传 body（POST/PUT 才有）
            req_body = None
            if self.command in ("POST", "PUT", "PATCH"):
                try:
                    cl = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    cl = 0
                if cl > 0:
                    req_body = self.rfile.read(cl)

            req = urllib.request.Request(target, data=req_body, method=self.command)
            for k, v in upstream_headers.items():
                req.add_header(k, v)

            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    raw = r.read()
                    ct = r.headers.get("Content-Type") or "application/octet-stream"
                    ce = r.headers.get("Content-Encoding")
                    self._reply(r.status, raw, ct, ce=ce)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read()
                except Exception:
                    body = b""
                ct = ((e.headers.get("Content-Type") if e.headers else None)
                      or "text/plain")
                ce = e.headers.get("Content-Encoding") if e.headers else None
                self._reply(e.code, body, ct, ce=ce)
        except Exception as e:
            try:
                self._reply(502,
                    f"upstream error: {type(e).__name__}: {e}".encode(),
                    "text/plain; charset=utf-8")
            except Exception:
                pass

    def _reply(self, status, body, ct, ce=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ct)
            if ce:
                self.send_header("Content-Encoding", ce)
            self.send_header("Content-Length", str(len(body) if body else 0))
            self.send_header("X-Proxied-By", "tencent-scf-hexun")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # SCF Web 函数 scf_bootstrap 默认期望进程监听 9000；
    # 少数模板用 PORT/SCF_RUNTIME_API_PORT 等环境变量
    port = int(os.environ.get("PORT") or os.environ.get("SCF_PORT") or "9000")
    sys.stderr.write(f"hexun-proxy listening on 0.0.0.0:{port}\n")
    sys.stderr.flush()
    with ThreadedHTTPServer(("0.0.0.0", port), ProxyHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
