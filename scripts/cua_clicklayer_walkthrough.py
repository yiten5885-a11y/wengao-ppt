#!/usr/bin/env python3
"""Cua 点击层复验（2026-11-04）。

目标：U1 验收项的「点击层」——用真实浏览器（本机已装的 Google Chrome，
Playwright 通过 CDP 驱动 DOM 级真实点击，不依赖 macOS 辅助功能/自动化权限）
对安装实例工作台（127.0.0.1:8823）做全链 UI 操作：

    打开首屏 → 上传 PPTX → 点击主题卡（先视觉升级组、再普通组）→
    点击「开始制作」→ 轮询五阶段（precheck→run→validate→publish→export）→
    等待下载区渲染 → 点击下载真实文件并核验 → 全程截图存证。

诚实边界：这是「真浏览器 UI 点击层」证据；WorkBuddy Cua 本体（AppleScript/
辅助功能层）仍被系统 TCC 拒绝（-10004），在 JSON 中如实登记，不冒名顶替。
"""

import json
import hashlib
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "reports" / "cua-clicklayer-20261104"
OUT.mkdir(parents=True, exist_ok=True)
INPUT = OUT / "input-variant.pptx"
BASE = "http://127.0.0.1:8823"

steps = []


def step(name, ok, detail, shot=None):
    rec = {"step": name, "ok": bool(ok), "detail": detail}
    if shot:
        rec["screenshot"] = shot
    steps.append(rec)
    print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def shot(page, name):
    p = OUT / name
    page.screenshot(path=str(p), full_page=True)
    return name


def main():
    verdict = "FAIL"
    job_id = None
    dl_info = None
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
            engine = "chrome channel (installed Google Chrome)"
        except Exception:
            browser = p.chromium.launch(
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                headless=True)
            engine = "chrome executable_path"
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  accept_downloads=True)
        page = ctx.new_page()
        print("engine:", engine, "| ua:", page.evaluate("navigator.userAgent"), flush=True)

        # 1. 打开首屏（bootstrap 渲染）
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#btn-make", timeout=30000)
        page.wait_for_timeout(1500)
        step("open_workbench", True, "工作台首屏渲染，#btn-make 就位",
             shot(page, "01-first-screen.png"))

        # 2. 上传输入件（真实 file input）
        page.set_input_files("#file", str(INPUT))
        page.wait_for_function(
            "() => !document.querySelector('#btn-make').disabled", timeout=30000)
        meta = page.inner_text("#file-meta").strip()
        step("upload_via_file_input", True, f"#file 上传成功，file-meta={meta!r}，"
             "「开始制作」由 disabled 变可用", shot(page, "02-uploaded.png"))

        # 3. 主题卡点击（两次真实点击：视觉升级组 → 普通组第一张）
        clicked = []
        upg = page.locator("#themes-visual-upgrade .theme").first
        if upg.count() > 0:
            upg.click()
            page.wait_for_timeout(400)
            clicked.append(upg.get_attribute("data-theme"))
            vis_hint = page.inner_text("#themes-visual-upgrade-hint")
            step("click_visual_upgrade_card", True,
                 f"点击视觉升级主题卡 data-theme={clicked[-1]!r}；"
                 f"可用性提示片段={vis_hint[:60]!r}")
        plain = page.locator("#themes .theme").first
        plain.click()
        page.wait_for_timeout(400)
        clicked.append(plain.get_attribute("data-theme"))
        state = page.inner_text("#theme-state").strip()
        step("click_plain_theme_card", True,
             f"点击普通主题卡 data-theme={clicked[-1]!r}，#theme-state={state!r}",
             shot(page, "03-theme-selected.png"))

        # 4. 点击「开始制作」（一键编排 precheck→run→validate→publish→export）
        page.click("#btn-make")
        try:
            page.wait_for_function(
                "() => document.querySelector('#make-note').textContent.includes('已受理')",
                timeout=20000)
            note = page.inner_text("#make-note").strip()
            job_id = note.split("任务 ")[-1].strip() if "任务 " in note else None
            step("click_btn_make", True, f"「开始制作」点击受理：{note}", shot(page, "04-make-accepted.png"))
        except Exception:
            note = page.inner_text("#make-note").strip()
            step("click_btn_make", False, f"点击后未受理：{note[:200]}",
                 shot(page, "04-make-rejected.png"))
            raise SystemExit(finish(verdict, job_id, dl_info))

        # 5. 轮询五阶段直到下载区渲染（终态判定以 UI 为准）
        deadline = time.time() + 480
        last_stages = ""
        mid_shot_done = False
        ready = False
        while time.time() < deadline:
            stages = page.inner_text("#make-stages").strip()
            if len(stages) > len(last_stages):
                last_stages = stages
            if not mid_shot_done and time.time() > deadline - 380:
                shot(page, "05-running-midway.png")
                mid_shot_done = True
            if page.inner_text("#downloads").strip() and \
               page.locator("#downloads a.btn").count() > 0:
                ready = True
                break
            if "未创建任务" in page.inner_text("#make-note"):
                break
            page.wait_for_timeout(5000)
        statusline = page.inner_text("#statusline").strip()
        step("stages_complete", ready,
             f"五阶段结束，statusline={statusline[:80]!r}；#downloads 链接数="
             f"{page.locator('#downloads a.btn').count()}",
             shot(page, "06-final-screen.png") if ready else shot(page, "06-stages-stuck.png"))

        # 6. 点击下载真实文件并核验
        if ready:
            links = page.locator("#downloads a.btn")
            target = None
            for i in range(links.count()):
                if links.nth(i).inner_text().strip() == "下载全部":
                    target = links.nth(i)
                    break
            if target is None:
                target = links.first
            with page.expect_download(timeout=60000) as dl:
                target.click()
            d = dl.value
            dest = OUT / "downloads" / d.suggested_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            d.save_as(str(dest))
            sha = hashlib.sha256(dest.read_bytes()).hexdigest()
            dl_info = {"file": dest.name, "bytes": dest.stat().st_size,
                       "sha256": sha[:16] + "…"}
            step("click_download", dest.stat().st_size > 0,
                 f"点击 {target.inner_text().strip()!r} 下载 {dest.name} "
                 f"({dest.stat().st_size} bytes, sha256={sha[:16]}…)",
                 shot(page, "07-download-clicked.png"))

        verdict = "PASS" if all(s["ok"] for s in steps) else "FAIL"
        finish(verdict, job_id, dl_info)
        browser.close()


def finish(verdict, job_id, dl_info):
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "acceptance": "U1 点击层（真浏览器 UI 全链）",
        "engine": "Playwright CDP -> 本机已装 Google Chrome（无头），DOM 级真实点击",
        "honest_boundary": "WorkBuddy Cua 本体（AppleScript/辅助功能）仍被系统 TCC "
                           "拒绝（osascript 报 -10004 权限违例，2026-11-04 实测）；"
                           "本证据是点击层等价验证，不冒名 Cua 本体。",
        "base_url": "http://127.0.0.1:8823",
        "input_sha256_12": hashlib.sha256(INPUT.read_bytes()).hexdigest()[:12],
        "job_id": job_id,
        "download": dl_info,
        "steps": steps,
        "verdict": verdict,
    }
    (OUT / "WALKTHROUGH.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("VERDICT:", verdict, flush=True)
    return verdict


if __name__ == "__main__":
    main()
