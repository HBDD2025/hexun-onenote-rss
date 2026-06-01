// 把 hexun.com 请求中转一下，绕开腾讯 EdgeOne 对 GitHub Actions IP 的拦截。
// 部署：dashboard.cloudflare.com → Workers & Pages → Create Worker → 把这段代码粘贴进去 → Deploy
// 部署后会得到一个形如 https://xxxx.workers.dev 的地址。
// 在 GitHub Repo Secrets 里把 HEXUN_PROXY_URL 设为：
//   https://xxxx.workers.dev/?url={url}
// 注意 {url} 占位符不要替换，Python 代码会自己替换成 urlencoded 的真实地址。

export default {
  async fetch(request) {
    const u = new URL(request.url);
    const target = u.searchParams.get("url");
    if (!target) {
      return new Response("usage: /?url=https%3A%2F%2Finsurance.hexun.com%2F...", { status: 400 });
    }
    // 安全：只代理 hexun.com，防止 worker 被滥用做开放代理
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response("invalid target url", { status: 400 });
    }
    if (!/(^|\.)hexun\.com$/i.test(targetUrl.hostname)) {
      return new Response("only *.hexun.com allowed", { status: 403 });
    }
    // 把上游请求的 headers 转发（剔掉 host/cf-*/cookie 等敏感或会冲突的）
    const headers = new Headers();
    for (const [k, v] of request.headers) {
      const lk = k.toLowerCase();
      if (lk === "host" || lk.startsWith("cf-") || lk === "x-forwarded-for" ||
          lk === "x-real-ip" || lk === "content-length") continue;
      headers.set(k, v);
    }
    // 强制设置一个常见浏览器 UA（如果上游没给，CF 默认 UA 又会被 WAF 识别）
    if (!headers.has("user-agent")) {
      headers.set("user-agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36");
    }
    if (!headers.has("accept")) {
      headers.set("accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8");
    }
    if (!headers.has("accept-language")) {
      headers.set("accept-language", "zh-CN,zh;q=0.9,en;q=0.8");
    }
    // 同站 referer
    if (!headers.has("referer")) {
      headers.set("referer", "https://insurance.hexun.com/");
    }

    try {
      const upstream = await fetch(targetUrl.toString(), {
        method: request.method,
        headers,
        // 透传 body（GET/HEAD 不会带）
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "follow",
      });
      // 把上游响应直接回吐；保留 content-type 与 content-encoding
      const respHeaders = new Headers();
      for (const [k, v] of upstream.headers) {
        const lk = k.toLowerCase();
        if (lk === "set-cookie" || lk === "transfer-encoding") continue;
        respHeaders.set(k, v);
      }
      respHeaders.set("x-proxied-by", "cf-worker-hexun");
      return new Response(upstream.body, {
        status: upstream.status,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response("upstream error: " + (err && err.message), { status: 502 });
    }
  },
};
