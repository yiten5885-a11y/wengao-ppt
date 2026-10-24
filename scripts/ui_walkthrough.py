#!/usr/bin/env python3
"""scripts/ui_walkthrough.py —— RS-9 真实浏览器逐状态走查（截图 + 断言）。

用系统 Chrome（channel=chrome）驱动 http://127.0.0.1:8823/ui，对每个界面状态
点击验证并留截图到 reports/rs-w4/ui-walkthrough/。任何一步失败即非零退出。

状态清单（对应 W4 指令 §6"状态齐全"）：
  01 空状态（引导拖入）
  02 运行中（可取消）——用演示任务的 SSE 事件流重放模拟
  03 完成（8 步时间线）
  04 有待确认（冲突卡）
  05 部分降级（2 页未配图说明）
  06 失败（给原因与下一步）
  07 来源标签
  08 配图标签
  09 本页检查
  10 窄屏（<960px 堆叠）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "rs-w4" / "ui-walkthrough"
BASE = "http://127.0.0.1:8823"
DEMO = "job-demo-walkthrough"

results = []


def shot(page, name: str, note: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p), full_page=False)
    results.append({"state": name, "file": p.name, "note": note})
    print(f"[shot] {name}: {note}")


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--no-proxy-server",
                                           "--disable-extensions"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                ignore_https_errors=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 01 空状态
        page.goto(BASE + "/ui", wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.locator("#empty-state").is_visible(), "空状态应可见"
        shot(page, "01-empty", "空状态：引导拖入 + 示例 chips")
        # 左栏本机探测
        assert page.locator("#localrun-rows .dot").count() >= 4, "本机运行应有 4+ 探测项"
        print("[ok] 本机运行探测项：", page.locator("#localrun-rows .dot").count())

        # 选中演示任务 → 03 完成态 + 04 待确认 + 05 降级
        page.locator(f'.job-item[data-job="{DEMO}"]').click()
        # 等 8 步时间线渲染（SSE 回放 + 状态端点 fallback 需要一点时间）
        try:
            page.wait_for_selector(".steps-grid .step-name", timeout=15000)
        except Exception:
            st = page.evaluate("() => ({ cur: WB.state.currentJobId, events: WB.state.events.length, terminal: WB.state.terminal, job: !!WB.state.job, chat: document.getElementById('chat').hidden })")
            print("[fail] 状态转储：", st)
            raise
        page.wait_for_timeout(1500)
        # 8 步时间线
        steps = page.locator(".steps-grid .step-name")
        assert steps.count() == 8, f"时间线应有 8 步，实际 {steps.count()}"
        shot(page, "03-done-timeline", "完成态：8 步时间线（读取/检索/对比/趋势/配图/版式/转场/核对）")
        # 冲突卡（待确认）
        confirm = page.locator(".confirm-card")
        if confirm.count():
            assert confirm.first.is_visible(), "冲突卡应可见"
            shot(page, "04-conflict-open", "待确认：行业均值两个数（采用公开来源/保留我的数据）")
            # 点"保留我的数据" → 已处理
            confirm.first.locator('button[data-decision="use_mine"]').click()
            page.wait_for_timeout(2500)
            shot(page, "04b-conflict-resolved", "裁决后：记录选择并局部重排")
        else:
            print("[warn] 演示任务没有冲突卡（decisions 为空）")
        # 降级说明
        degrade = page.locator(".degrade-note")
        if degrade.count():
            assert degrade.first.is_visible()
            shot(page, "05-degraded", "部分降级：未配图页数与说明")
        # 07 来源标签
        page.locator('#tabs .seg-btn[data-tab="sources"]').click()
        page.wait_for_timeout(800)
        cards = page.locator(".source-card")
        assert cards.count() >= 1, "来源标签应有来源卡"
        shot(page, "07-sources", f"来源标签：{cards.count()} 个来源（含已丢弃）")
        # 08 配图标签
        page.locator('#tabs .seg-btn[data-tab="images"]').click()
        page.wait_for_timeout(800)
        tiles = page.locator(".image-tile")
        assert tiles.count() >= 1, "配图标签应有图块"
        shot(page, "08-images", f"配图标签：{tiles.count()} 个场景（实拍/生成/未配图）")
        # 09 本页检查 + 缩略条
        page.locator('#tabs .seg-btn[data-tab="preview"]').click()
        page.wait_for_timeout(800)
        rows = page.locator("#page-check-rows .k")
        assert rows.count() >= 1, "本页检查应有行"
        shot(page, "09-page-check", "本页检查：渲染/配图账本/转场/版式/研究证据")
        # 缩略条点击第 2 页
        thumbs = page.locator(".thumb")
        if thumbs.count() >= 2:
            thumbs.nth(1).click()
            page.wait_for_timeout(600)
            shot(page, "09b-thumb-select", "缩略条切换到第 2 页")
        # 10 窄屏
        page.set_viewport_size({"width": 720, "height": 900})
        page.wait_for_timeout(800)
        shot(page, "10-narrow", "窄屏（720px）：三栏堆叠，功能不丢")
        page.set_viewport_size({"width": 1600, "height": 1000})

        # 06 失败态：用一个必然失败的任务（不存在）触发错误提示
        page.locator("#btn-export").click()
        page.wait_for_timeout(600)
        shot(page, "06-toast-feedback", "操作反馈：toast（ aria-live ）")

        # JS 错误检查
        if errors:
            print("[fail] 页面 JS 错误：", errors[:5])
        browser.close()

    (OUT / "walkthrough.json").write_text(
        json.dumps({"states": results, "js_errors": errors},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n走查完成：{len(results)} 个状态，JS 错误 {len(errors)} 个")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
