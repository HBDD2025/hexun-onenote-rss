# hexun-onenote-rss

每天北京时间 **06:00 / 12:00 / 17:00** 自动把 hexun 保险行业资讯栏目新发布的文章推送到你的 OneNote 指定分区。

文章标题格式：`YYYYMMDD标题`（与原标题直接拼接，例：`20260525152亿元！商保扛起创新药械支付重担…`）
正文保留原网页的加粗/斜体/链接/列表/表格，**图片下载后嵌入到原始位置**。

---

## 一次性配置（按顺序做完，约 20 分钟）

### Step 1 — 在 Azure 注册一个免费应用

> 这一步是为了让 OneNote API 知道"是谁在调它"。一个 Client ID，免费，永久有效。

1. 用你的 Microsoft 账号（outlook / hotmail / live）登录 https://entra.microsoft.com
2. 左侧菜单 → **应用程序 (Applications)** → **应用注册 (App registrations)** → **新注册 (New registration)**
3. 填表：
   - **名称**：`hexun-onenote-rss`（随便）
   - **支持的账户类型**：选 **"仅个人 Microsoft 账户 (Personal Microsoft accounts only)"**
   - **重定向 URI**：留空
4. 点 **注册**
5. 注册后会跳到应用概览页面，**记下"应用程序（客户端）ID"** —— 后面要用
6. 左侧菜单 → **身份验证 (Authentication)** → 拉到底 → **高级设置 (Advanced settings)** → **允许公用客户端流 (Allow public client flows)** 设为 **是**，保存
7. 左侧菜单 → **API 权限 (API permissions)** → **添加权限 (Add a permission)** → **Microsoft Graph** → **委托的权限 (Delegated permissions)** → 搜索 `Notes` → 勾上 **`Notes.ReadWrite`** → 添加权限

### Step 2 — 本地跑一次 setup.py

> 用 Step 1 的 Client ID 做浏览器扫码登录，拿到 refresh_token 和你要写入的分区 ID。

```bash
cd ~/Desktop/hexun-onenote-rss
python3 setup.py
```

按提示操作：
1. 粘贴 Client ID
2. 终端会显示一个 8 位码 + 网址。打开网址，输码，登录你的微软账号，同意权限
3. 终端列出你所有 OneNote 分区，输入想要的分区编号
4. 选择是否推一条"配置测试"页验证 — 推荐选 Y。然后去 OneNote 看看那个分区里有没有那条"【配置测试】"页面，有就说明全链路通了

跑完后终端会打印 3 个值，本地也存了一份 `~/hexun-onenote-secrets.json`：

```
AZURE_CLIENT_ID:     xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MS_REFRESH_TOKEN:    M.C5xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx...（很长）
ONENOTE_SECTION_ID:  0-1234567890ABCDEF!12345
```

> ⚠️ `MS_REFRESH_TOKEN` 是密码级别的凭据，别截图、别贴聊天。

### Step 3 — 创建 GitHub 仓库并推上去

1. 在 GitHub 建一个新仓库，**Private**，名字随便（比如 `hexun-onenote-rss`）
2. 在本机：

```bash
cd ~/Desktop/hexun-onenote-rss
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin git@github.com:<你的用户名>/hexun-onenote-rss.git
git push -u origin main
```

3. 打开仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**，依次新建 3 个：

| Name | Value |
|------|-------|
| `AZURE_CLIENT_ID` | Step 2 拿到的 Client ID |
| `MS_REFRESH_TOKEN` | Step 2 拿到的 refresh_token |
| `ONENOTE_SECTION_ID` | Step 2 拿到的 section_id |

### Step 4 — 验证 Actions 能跑

1. 打开仓库 → **Actions** 选项卡 → 左侧选中 `hexun-onenote-push` → 右侧 **Run workflow** → 选 main 分支 → **Run workflow**
2. 等 1–2 分钟，刷新看绿色对勾。失败的话点进去看日志
3. 去 OneNote 看那个分区，应该有新页面出现（首跑只追溯过去 48 小时）

之后就交给 cron 自动跑了，不用再管。

---

## 日常使用须知

- **GitHub Actions 调度延迟**：cron 时间不精准，高峰时段（如 00:00 UTC）会延迟 5–30 分钟。如果对时点不敏感问题不大
- **去重**：用 `state.json` 记录已推送 URL，Actions 每次跑完会自动 commit 回仓库
- **冷启动**：第一次跑只推过去 48 小时的文章，更老的记入 state 不再推送
- **失败如何知道**：任何文章推送失败、列表抓取失败、API 调用失败，都会以 `【ERROR】` 开头的页面推送到同一个 OneNote 分区
- **手动跑一次**：去 Actions 页面点 **Run workflow** 就行
- **refresh_token 寿命**：理论上微软每次刷新会返回新的 token，脚本会在日志里提醒；个人账号通常几个月都有效，到期了重跑一次 `setup.py` 拿新的更新到 Secrets 即可

## 项目文件
- `daily_push.py` 主程序（Actions 跑这个）
- `hexun_lib.py` 和讯抓取
- `body_xhtml.py` 正文清洗
- `onenote.py` Graph API
- `setup.py` 本地一次性配置
- `state.json` 已推送 URL 列表
- `.github/workflows/push.yml` cron + 任务定义

## 常见坑

| 现象 | 原因 / 修法 |
|------|-----|
| setup.py 提示 "AADSTS50059" / "tenant" | Azure 应用没选"仅个人 Microsoft 账户"。回 Step 1 第 3 条改一下 |
| setup.py 列不到分区 | 该微软账号下还没有 OneNote 笔记本；先打开 onenote.com 创建一个分区 |
| Actions 失败 "invalid_grant" | refresh_token 过期或被吊销。本地重跑 `setup.py` 拿新 token，更新 Secret 即可 |
| OneNote 页面里图全是裂图 | 图片源站(toutiao CDN)防盗链。`hexun_lib.fetch_binary` 已带 Referer；若仍不行，看 Actions 日志里 "图片下载失败" 的错误 |
| 同一篇文章重复推送 | state.json 没 commit 回来。检查 Actions 日志末尾 "Commit state.json" 那步是否成功 |
