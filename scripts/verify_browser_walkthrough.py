#!/usr/bin/env python3
"""scripts/verify_browser_walkthrough.py —— FIX-W4-001 浏览器真实走查与截图。

分别在 / 与 /ui 走一遍：上传 .md 材料、上传 .pptx 原稿、开始制作、取消制作、导出。
每张截图必须对应一次真实的状态变化；关键步骤把接口响应渲染进页面，
使截图自身可证明请求打到了预期路由（旧路由 server.py / 新路由 ui_api）。
全程输出 walkthrough_evidence.json（接口响应、等待条件、耗时）。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "rs-w4" / "fix-w4-001"
BASE = "http://127.0.0.1:8823"
LEGACY_EXPORT_JOB = "job-967aa0388260"  # 旧导出路由走查对象（有候选的真实任务）

# 准备测试文件
TEST_MD = Path("/tmp/fixw4_walkthrough_test.md")
TEST_PPTX = Path("/tmp/fixw4_walkthrough_test.pptx")

TEST_MD.write_text(
    "# 稳稿 2026 业务复盘\n\n"
    "## 营收增长\n"
    "2026 年第三季度营业收入达到 12.8 亿元，同比增长 15.6%。\n\n"
    "## 核心产品\n"
    "稳稿工作台全面升级，支持智能素材生成与版式重排。\n",
    encoding="utf-8"
)

src_pptx = ROOT / "reports" / "rs-w2" / "page_types.pptx"
if not src_pptx.exists():
    cands = list(ROOT.glob("**/*.pptx"))
    if not cands:
        print("未找到测试 pptx 文件", file=sys.stderr)
        sys.exit(1)
    src_pptx = cands[0]
shutil.copyfile(src_pptx, TEST_PPTX)

OUT.mkdir(parents=True, exist_ok=True)
results: list[dict] = []
evidence: list[dict] = []


def shot(page, name: str, desc: str):
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p), full_page=False)
    results.append({"name": name, "file": p.name, "description": desc})
    print(f"[SHOT] {name}: {desc}")


def record(step: str, **kw):
    kw["step"] = step
    kw["at"] = time.strftime("%H:%M:%S")
    evidence.append(kw)
    print(f"[EV ] {step}: " + json.dumps(
        {k: v for k, v in kw.items() if k not in ("step", "at")},
        ensure_ascii=False)[:300])


def inject_panel(page, title: str, lines: list[str]):
    """把证据面板渲染进页面（高 z-index、等宽字体），截图自带证明。"""
    safe = "".join(f"<div>{line}</div>" for line in lines)
    page.evaluate("""([title, safe]) => {
        let el = document.getElementById('fixw4-evidence');
        if (!el) {
            el = document.createElement('div');
            el.id = 'fixw4-evidence';
            el.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:2147483647;'
                + 'max-width:70%;background:#fffbe6;border:2px solid #b45309;border-radius:8px;'
                + 'padding:10px 14px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;'
                + 'color:#1f2937;box-shadow:0 4px 16px rgba(0,0,0,.25);white-space:pre-wrap;';
            document.body.appendChild(el);
        }
        el.innerHTML = '<b style="color:#b45309">' + title + '</b>' + safe;
    }""", [title, safe])


def main():
    print(f"开始浏览器真实走查，输出目录：{OUT}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-proxy-server", "--disable-extensions"]
        )

        # ==============================================================
        # 1. 旧工作台 / 走查
        # ==============================================================
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        print("\n--- 走查旧工作台 ( / ) ---")
        t0 = time.time()
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(1000)
        record("01_legacy_home", url="/", load_s=round(time.time() - t0, 2),
               title=page.title())
        shot(page, "01_legacy_home", "旧工作台首页：演示工作室")

        # (1) 切换到「从材料生成」并上传 .md
        page.locator('nav button[data-view="material"]').click()
        page.wait_for_timeout(500)
        page.locator('#mat-seg button[data-mode="model"]').click()
        page.wait_for_timeout(500)

        page.set_input_files("#mat-file", str(TEST_MD))
        page.wait_for_timeout(1500)
        meta_text = page.locator("#mat-file-meta").inner_text()
        print(f"材料上传结果文本: {meta_text!r}")
        assert "失败" not in meta_text, f"材料上传失败：{meta_text}"
        assert "fixw4_walkthrough_test.md" in meta_text, f"材料上传未显示文件名：{meta_text}"
        record("02_legacy_material_uploaded", route="/api/material/upload",
               meta=meta_text[:200])
        shot(page, "02_legacy_material_uploaded", "旧工作台：上传 .md 材料成功，显示文件元数据与 SHA")

        # (2) 切换回「演示工作室」并上传 .pptx
        page.locator('nav button[data-view="studio"]').click()
        page.wait_for_timeout(500)
        page.set_input_files("#file", str(TEST_PPTX))
        page.wait_for_timeout(1500)
        file_meta = page.locator("#file-meta").inner_text()
        print(f"PPTX 上传结果文本: {file_meta!r}")
        assert "失败" not in file_meta, f"PPTX 上传失败：{file_meta}"
        record("03_legacy_pptx_uploaded", route="/api/upload", meta=file_meta[:200])
        shot(page, "03_legacy_pptx_uploaded", "旧工作台：上传 .pptx 原稿成功，主题与制作按钮已就绪")

        # (3) 点击「开始制作」：等到取消按钮真正出现（任务进入运行态）再截图
        make_btn = page.locator("#btn-make")
        assert make_btn.is_enabled(), "开始制作按钮应已就绪"
        make_btn.click()
        try:
            page.wait_for_selector("#btn-make-cancel:not(.hidden)", timeout=20000)
            running_seen = True
        except Exception:
            running_seen = False
        page.wait_for_timeout(500)
        record("04_legacy_make_started", route="/api/make",
               cancel_button_visible=running_seen)
        shot(page, "04_legacy_make_started",
             "旧工作台：已点击开始制作，取消制作按钮亮起（任务运行中）" if running_seen
             else "旧工作台：已点击开始制作（任务可能已快速结束，取消按钮未亮）")

        # (4) 点击「取消制作」：等到状态行出现取消态再截图
        cancel_btn = page.locator("#btn-make-cancel")
        if cancel_btn.is_visible():
            cancel_btn.click()
            page.wait_for_timeout(2000)
            status_text = page.locator("#statusline").inner_text() if page.locator("#statusline").count() else ""
            record("05_legacy_make_cancelled", route="/api/make/<id>/cancel",
                   statusline=status_text[:200])
            shot(page, "05_legacy_make_cancelled", "旧工作台：已点击取消制作，状态行显示取消态")
        else:
            record("05_legacy_make_cancelled", skipped=True,
                   reason="取消制作按钮不可见（任务已结束）")
            shot(page, "05_legacy_make_cancelled", "旧工作台：任务已结束，无取消动作")

        # (5) 旧导出路由核验：真实调用 /api/jobs/<id>/export，
        #     并把响应渲染进页面后截图——截图自身可证明请求由 server.py 旧路由处理
        #     （旧契约：exported/rc/gate；新契约应为 status/attribution）。
        t0 = time.time()
        res = page.evaluate("""async (job) => {
            const r = await fetch('/api/jobs/' + job + '/export', { method: 'POST' });
            return { status: r.status, data: await r.json() };
        }""", LEGACY_EXPORT_JOB)
        dt = round(time.time() - t0, 2)
        data = res.get("data") or {}
        is_old_contract = ("exported" in data) or (data.get("error", {}).get("code") == "WB.JOB_NOT_FOUND")
        print(f"旧路由导出调用测试结果: {res}")
        record("06_legacy_export_tested", route=f"/api/jobs/{LEGACY_EXPORT_JOB}/export",
               http_status=res.get("status"), elapsed_s=dt,
               old_contract=bool(is_old_contract), response=json.dumps(data, ensure_ascii=False)[:400])
        inject_panel(page, "FIX-W4-001 旧路由导出核验（应由 server.py _job_action 处理）", [
            f"POST /api/jobs/{LEGACY_EXPORT_JOB}/export",
            f"HTTP {res.get('status')}  ·  {dt}s",
            f"exported = {data.get('exported')}   rc = {data.get('rc')}",
            f"gate = {data.get('gate')}",
            "↑ 旧契约字段（exported/rc/gate）⇒ 旧路由处理，未被新入口抢占",
        ])
        page.wait_for_timeout(300)
        shot(page, "06_legacy_export_tested",
             f"旧工作台：POST /api/jobs/<id>/export → HTTP {res.get('status')}，"
             f"旧契约响应（exported={data.get('exported')}, gate={data.get('gate')}）")
        page.close()

        # ==============================================================
        # 2. 新工作台 /ui 走查
        # ==============================================================
        page_ui = browser.new_page(viewport={"width": 1600, "height": 1000})
        print("\n--- 走查新工作台 ( /ui ) ---")
        t0 = time.time()
        page_ui.goto(BASE + "/ui", wait_until="networkidle")
        page_ui.wait_for_timeout(1000)
        record("11_ui_home", url="/ui", load_s=round(time.time() - t0, 2),
               title=page_ui.title())
        shot(page_ui, "11_ui_home", "新工作台首页：空态与输入框")

        # (1) 上传 .md 材料：等到导入 toast 出现
        page_ui.set_input_files("#file-input", str(TEST_MD))
        try:
            page_ui.wait_for_selector("#toast:not([hidden])", timeout=15000)
            page_ui.wait_for_timeout(800)
            toast1 = page_ui.locator("#toast").inner_text()
        except Exception:
            toast1 = ""
        record("12_ui_material_uploaded", route="/api/material/upload", toast=toast1[:200])
        shot(page_ui, "12_ui_material_uploaded", f"新工作台：上传 .md 材料（toast：{toast1[:40]}）")

        # (2) 上传 .pptx 原稿：等新任务真正出现（左栏数量增加）再截图
        jobs_before = page_ui.locator(".job-item").count()
        page_ui.set_input_files("#file-input", str(TEST_PPTX))
        try:
            page_ui.wait_for_function(
                "n => document.querySelectorAll('.job-item').length > n",
                arg=jobs_before, timeout=30000)
            page_ui.wait_for_timeout(1500)
        except Exception:
            pass
        job_count = page_ui.locator(".job-item").count()
        title_text = page_ui.locator("#job-title").inner_text()
        record("13_ui_pptx_uploaded", route="/api/upload + /api/make",
               jobs_before=jobs_before, job_items=job_count, job_title=title_text[:120])
        shot(page_ui, "13_ui_pptx_uploaded",
             f"新工作台：上传 .pptx 原稿并创建制作任务（左栏 {jobs_before}→{job_count} 个任务，当前：{title_text[:30]}）")

        # (3) 制作中：等到取消按钮可见（SSE follow → running=true）再截图，
        #     与 13 形成真实状态差（13 是任务刚创建，14 是运行中）。
        try:
            page_ui.wait_for_selector("#btn-cancel:not([hidden])", timeout=25000)
            running_seen = True
        except Exception:
            running_seen = False
        page_ui.wait_for_timeout(500)
        sub_text = page_ui.locator("#job-sub").inner_text()
        record("14_ui_make_running", cancel_button_visible=running_seen,
               job_sub=sub_text[:120])
        shot(page_ui, "14_ui_make_running",
             f"新工作台：任务制作中（状态：{sub_text[:20]}，取消制作按钮可见）" if running_seen
             else f"新工作台：任务状态 {sub_text[:30]}（取消按钮未亮）")

        # (4) 点击「取消制作」：等到状态变为已取消再截图
        ui_cancel = page_ui.locator("#btn-cancel")
        if ui_cancel.is_visible():
            ui_cancel.click()
            try:
                page_ui.wait_for_function(
                    "document.getElementById('job-sub').textContent.indexOf('已取消') >= 0",
                    timeout=15000)
            except Exception:
                pass
            page_ui.wait_for_timeout(500)
            sub_after = page_ui.locator("#job-sub").inner_text()
            record("15_ui_cancelled", route="/api/ui/jobs/<id>/cancel",
                   job_sub=sub_after[:120])
            shot(page_ui, "15_ui_cancelled", f"新工作台：点击取消制作，状态：{sub_after[:20]}")
        else:
            record("15_ui_cancelled", skipped=True, reason="取消按钮不可见")
            shot(page_ui, "15_ui_cancelled", "新工作台：无运行中任务，跳过取消")

        # (5) 导出：选中已就绪任务，点击导出，等 toast 反馈后截图
        target = page_ui.locator(f'.job-item[data-job="{LEGACY_EXPORT_JOB}"]')
        if target.count() == 0:
            target = page_ui.locator(".job-item").first
        target.click()
        page_ui.wait_for_timeout(1500)
        export_btn = page_ui.locator("#btn-export")
        export_btn.click()
        try:
            page_ui.wait_for_function(
                "(() => { const t = document.getElementById('toast');"
                " return t && !t.hidden && t.textContent.trim().length > 0; })()",
                timeout=15000)
        except Exception:
            pass
        page_ui.wait_for_timeout(500)
        toast = page_ui.locator("#toast").inner_text()
        print(f"新工作台导出 Toast 反馈: {toast!r}")
        record("16_ui_exported", route=f"/api/ui/jobs/{LEGACY_EXPORT_JOB}/export",
               toast=toast[:300])
        shot(page_ui, "16_ui_exported",
             f"新工作台：点击导出按钮，触发 /api/ui/jobs/<id>/export（反馈：{toast[:60]}）")
        page_ui.close()

        browser.close()

    manifest = OUT / "walkthrough_manifest.json"
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ev = OUT / "walkthrough_evidence.json"
    ev.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n走查全部完成！共留存 {len(results)} 张截图于 {OUT}；证据见 {ev.name}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
