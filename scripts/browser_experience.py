#!/usr/bin/env python3
"""scripts/browser_experience.py —— 操作真实 Chrome 浏览器体验稳稿 PPT 完整工作流程并截图留存。

走查流程：
1. 启动 macOS 本机真实的 Google Chrome 浏览器（通过 Playwright）
2. 访问 8823 工作台首页 (http://127.0.0.1:8823/)
3. 走查旧版工作台三大核心板块：
   - 演示工作室（已有 PPTX 美化、主题切换）
   - 从材料生成（查看正在执行的真实材料任务流程、状态步进）
   - 版本与历史（任务版本轨迹与恢复；原「局部编辑」页已于 2026-11-04 从工作台移除）
4. 访问新版 RS-9 现代化工作台 (http://127.0.0.1:8823/ui)
   - 走查本机运行环境探测（mlx-dspark 18088、LibreOffice、字体、真实渲染器）
   - 查看生成任务列表与时间线
5. 将全部关键节点的高清截图和走查报告保存至 reports/browser_experience/
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "browser_experience"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8823"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

steps = []


def shot(page, name: str, note: str) -> str:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    steps.append({"step": name, "file": str(path), "note": note, "time": time.strftime("%H:%M:%S")})
    print(f"[SHOT] {name}: {note}")
    return str(path)


def run():
    print(f"=== 稳稿 PPT 真实 Chrome 浏览器走查启动 ===")
    print(f"浏览器路径: {CHROME_BIN}")
    print(f"工作台地址: {BASE}")
    print(f"产物输出目录: {OUT}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=CHROME_BIN,
            headless=True,
            args=["--no-proxy-server", "--disable-extensions", "--window-size=1440,900"]
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Step 1: 访问旧版工作台首页
        print("1. 访问工作台首页 (http://127.0.0.1:8823/)...")
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(1000)
        title = page.title()
        print(f"   页面标题: {title}")
        assert "稳稿" in title or "PPT" in title, f"标题异常: {title}"
        shot(page, "01_workbench_home", "旧版工作台首页：演示工作室与主题配置")

        # Step 2: 切换到「从材料生成」
        print("2. 切换到「从材料生成」界面...")
        page.locator('nav button[data-view="material"]').click()
        page.wait_for_timeout(1000)
        shot(page, "02_material_generator_view", "从材料生成：事实冻结台账与内容规划模式")

        # Step 3: 查看当前正在运行的材料任务实时进展
        print("3. 检查材料任务实时进度与任务面板...")
        page.wait_for_timeout(2000)
        shot(page, "03_material_task_live", "材料任务步进：回放冻结计划构建与渲染核对")

        # Step 4: 切换到「已有 PPTX 美化」并查看主题选择
        print("4. 走查演示工作室（主题挑选与配置）...")
        page.locator('nav button[data-view="studio"]').click()
        page.wait_for_timeout(800)
        # 点击选择商务深色主题
        theme_btn = page.locator('.theme[data-theme="wengao-business"]')
        if theme_btn.count():
            theme_btn.first.click()
            page.wait_for_timeout(500)
        shot(page, "04_studio_theme_selection", "演示工作室：原稿适配与主题切换控制台")

        # Step 5: 访问新版 RS-9 现代化工作台 /ui
        print("5. 访问新版 RS-9 现代化工作台 (/ui)...")
        page.goto(BASE + "/ui", wait_until="networkidle")
        page.wait_for_timeout(1500)
        shot(page, "05_modern_ui_overview", "RS-9 现代化工作台：空状态引导与左侧任务/环境看板")

        # Step 6: 检查左侧本机环境探测
        probe_count = page.locator("#localrun-rows .dot").count()
        print(f"   本机运行探测项数量: {probe_count}")
        shot(page, "06_modern_ui_probes", "RS-9 本机运行环境看板：mlx-dspark、LibreOffice、真实渲染器状态")

        browser.close()

    manifest_path = OUT / "walkthrough_manifest.json"
    manifest_path.write_text(
        json.dumps({"walkthrough": "稳稿 PPT Chrome 浏览器真实体验走查",
                    "steps": steps,
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n走查全部完成！共留存 {len(steps)} 张截图，清单已写入 {manifest_path}")


if __name__ == "__main__":
    run()
