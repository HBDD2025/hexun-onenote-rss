#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地一次性配置：拿到 refresh_token 和 section_id，给你三个值用于 GitHub Secrets。

跑法：
    python3 setup.py

需要你先去 https://entra.microsoft.com 注册 Azure 应用（README 有步骤），
拿到 Client ID 后才能跑这个脚本。
"""

import getpass
import json
import os
import sys

import onenote


def main():
    print("=" * 64)
    print("和讯 → OneNote 推送 · 一次性本地配置向导")
    print("=" * 64)
    print()
    print("第 1 步：粘贴你在 Azure 注册的 Client ID（应用程序客户端 ID）")
    print("        长这样：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    client_id = input("Client ID: ").strip()
    if not client_id or len(client_id) < 30:
        print("Client ID 看起来不对，退出。")
        sys.exit(1)

    print()
    print("第 2 步：浏览器授权登录")
    print("-" * 64)
    code = onenote.device_code_start(client_id)
    print(code.get("message") or
          f"打开 {code['verification_uri']} 输入码 {code['user_code']}")
    print("等待你完成浏览器授权...")
    tokens = onenote.device_code_poll(
        client_id, code["device_code"],
        interval=code.get("interval", 5),
        expires_in=code.get("expires_in", 900),
    )
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("没拿到 refresh_token，请确认 scope 里包含 offline_access。")
        sys.exit(1)
    print("授权成功。")

    print()
    print("第 3 步：选一个要写入的 OneNote 分区")
    print("-" * 64)
    sections = onenote.list_sections(access_token)
    if not sections:
        print("没列到任何分区。请先在 OneNote 里手动建一个分区，然后重跑。")
        sys.exit(1)
    for i, s in enumerate(sections):
        print(f"  [{i:2d}]  {s['notebookName']} > {s['displayName']}")
    while True:
        raw = input("\n输入要写入的分区编号: ").strip()
        try:
            idx = int(raw)
            chosen = sections[idx]
            break
        except (ValueError, IndexError):
            print("编号不对，重试。")
    section_id = chosen["id"]

    print()
    print("=" * 64)
    print("配置完成。请把下面 3 个值粘到 GitHub 仓库的 Settings → Secrets and variables → Actions：")
    print("=" * 64)
    print(f"\n  AZURE_CLIENT_ID:")
    print(f"  {client_id}\n")
    print(f"  MS_REFRESH_TOKEN:")
    print(f"  {refresh_token}\n")
    print(f"  ONENOTE_SECTION_ID:")
    print(f"  {section_id}\n")
    print("=" * 64)

    # 本地也存一份方便复制（不要 commit 到仓库）
    out_path = os.path.expanduser("~/hexun-onenote-secrets.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "AZURE_CLIENT_ID": client_id,
            "MS_REFRESH_TOKEN": refresh_token,
            "ONENOTE_SECTION_ID": section_id,
            "section_display": f"{chosen['notebookName']} > {chosen['displayName']}",
        }, f, indent=2, ensure_ascii=False)
    print(f"也保存到本地：{out_path}")
    print("（这个文件含敏感凭据，不要 commit 到任何 Git 仓库）")

    print()
    ans = input("是否现在试着推送一条测试页面到该分区？(Y/n): ").strip().lower()
    if ans != "n":
        from datetime import datetime, timezone, timedelta
        beijing = timezone(timedelta(hours=8))
        now = datetime.now(beijing)
        title = f"{now.strftime('%Y%m%d')}【配置测试】和讯 OneNote 推送已就绪"
        body = (
            f"<p>这是一条来自 setup.py 的测试页面。</p>"
            f"<p>分区：{chosen['notebookName']} &gt; {chosen['displayName']}</p>"
            f"<p>本地时间（北京）：{now.isoformat()}</p>"
            f"<p>如果你能在 OneNote 里看到这一页，下一步去配 GitHub Actions 就行。</p>"
        )
        try:
            onenote.create_page(access_token, section_id, title, body, [], created_iso=now.isoformat())
            print("已推送测试页。打开 OneNote 查看吧。")
        except Exception as e:
            print(f"测试推送失败：{e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
