# 和讯保险 5 栏目 TXT 批量抓取

跟主项目（OneNote RSS 同步）共用一个仓库，但功能完全独立。

## 用途
按指定日期范围，把和讯保险 5 个栏目（行业资讯/监管动态/公司新闻/中介营销/市场评论）的全部文章正文合并成一个 TXT 文件。跨栏目同标题的文章自动去重。

可抓时段约 **2018-01 至今**。

## 用法

在新电脑（macOS）上：

```bash
# 1. 把这两个文件移到桌面（双击 .command 文件需要执行权限）
cp hexun_scraper.py 保险新闻抓取.command ~/Desktop/
chmod +x ~/Desktop/保险新闻抓取.command ~/Desktop/hexun_scraper.py

# 2. 双击 ~/Desktop/保险新闻抓取.command
#    首次双击若被 Gatekeeper 拦：右键 → 打开 → 允许
```

按提示输入起始日期、结束日期、输出文件路径，等它跑完。

## 依赖
- macOS（`.command` 文件是 Mac 专属）
- Python 3（系统自带 `/usr/bin/python3`；没有的话 `xcode-select --install`）
- 能上外网

无第三方 pip 依赖，纯标准库。
