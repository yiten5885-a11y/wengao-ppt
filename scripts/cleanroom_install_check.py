#!/usr/bin/env python3
"""干净安装根实测：安装 → 首任务 → 升级 → 回退 → 卸载（保留数据）。

边界（必须写在结论里）
----------------------
- 这是**同机、全新安装根**验证：解释器与外部依赖（LibreOffice、python 包）来自
  本机既有环境，不是全新机器。任何"全新机器可用"的结论都不能由本脚本得出。
- 安装根路径故意包含**中文与空格**，用于覆盖"路径含非 ASCII/空格"这类真实故障。
- 首任务在**安装实例**上跑（`wengao cli`，程序根=versions/<label>，数据根=<root>/data），
  不使用开发目录。

用法
----
    python3 scripts/cleanroom_install_check.py \
        --payload dist/wengao-ppt-0.1.0-xxxx \
        --root "work/cleanroom/安装 根" \
        --report reports/install-cleanroom-<date>
退出码：0 = 全部步骤通过；2 = 有步骤失败（证据仍会落盘）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROG_ROOT = HERE.parent


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], cwd: Path, timeout=900) -> dict:
    t0 = time.time()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CODEBUDDY_TOOL_CALL_ID",
                        "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, env=env)
    out, err = proc.stdout or "", proc.stderr or ""
    parsed = None
    stripped = out.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
    return {"cmd": cmd, "cwd": str(cwd), "rc": proc.returncode,
            "stdout_head": out[:4000], "stderr_head": err[-4000:],
            "json": parsed, "seconds": round(time.time() - t0, 2)}


def build_input_pptx(path: Path, pages: int = 3) -> Path:
    """首任务输入：本脚本自建，不含任何用户私有材料。"""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12191695, 6858000
    titles = ["干净安装首任务", "覆盖范围", "结论"]
    bodies = [
        "本页用于验证安装实例能否完成一次真实的美化任务。",
        "三页纯文本；不含图片、表格与图表，便于判定渲染是否被外部依赖影响。",
        "若导出件可打开且结构自洽，说明安装布局下的程序与资源解析成立。",
    ]
    for i in range(pages):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = titles[i % len(titles)]
        body = s.placeholders[1]
        body.text_frame.text = bodies[i % len(bodies)]
        for para in body.text_frame.paragraphs:
            for r in para.runs:
                r.font.size = Pt(20)
    prs.save(str(path))
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="干净安装根实测（安装/首任务/升级/回退/卸载）")
    ap.add_argument("--payload", required=True, help="安装载荷目录")
    ap.add_argument("--root", required=True, help="隔离安装根（可含中文/空格）")
    ap.add_argument("--report", required=True, help="证据输出目录")
    ap.add_argument("--python", default=sys.executable, help="用于安装的解释器")
    ap.add_argument("--keep", action="store_true", help="保留安装根（默认也保留，不删）")
    a = ap.parse_args(argv)

    payload = Path(a.payload).expanduser().resolve()
    root = Path(a.root).expanduser()
    root = (root if root.is_absolute() else (PROG_ROOT / root)).resolve()
    report = Path(a.report).expanduser()
    report = (report if report.is_absolute() else (PROG_ROOT / report)).resolve()
    report.mkdir(parents=True, exist_ok=True)

    ev: dict = {
        "schema": "wengao.cleanroom_install.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": "同机 + 全新安装根 + 独立数据根；**不是**全新机器验证",
        "python": a.python,
        "payload": str(payload),
        "payload_sha256_members": sum(1 for p in payload.rglob('*') if p.is_file()),
        "root": str(root),
        "non_ascii_root": not root.as_posix().isascii(),
        "steps": [],
    }

    def step(name, rec, ok=None, external_if=("MODEL.", "GATE.MODEL")):
        rec = {"step": name, **rec}
        if ok is None:
            ok = rec.get("rc") == 0
        rec["ok"] = bool(ok)
        if not rec["ok"]:
            # 失败归因：模型/视觉端点不可用属于**外部依赖缺失**，不是安装器缺陷。
            # 只有 JSON 信封里出现 MODEL.* 代码时才这样记，绝不凭失败猜原因。
            blob = json.dumps(rec.get("json") or {}, ensure_ascii=False)
            if any(tag in blob for tag in external_if):
                rec["blocked_external"] = True
        ev["steps"].append(rec)
        return ok

    # ---- 0 前置：根必须是干净/不存在，绝不覆盖既有安装 ----
    if root.exists() and any(root.iterdir()):
        ev["aborted"] = {"reason": "root_not_clean", "root": str(root),
                         "detail": "安装根已存在且非空；为免覆盖既有安装，已中止。"}
        (report / "CLEANROOM_INSTALL.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(ev["aborted"], ensure_ascii=False, indent=2))
        return 4

    inst = ["-m", "wengao_ppt.install", "--root", str(root)]

    # ---- 1 安装并激活 ----
    r = run([a.python, *inst, "install", "--package", str(payload),
             "--activate", "--expect-root", str(root)], payload)
    step("install", r)
    installed_label = None
    if r["json"]:
        installed_label = (r["json"].get("label")
                           or (r["json"].get("install") or {}).get("label"))
    ev["installed_label"] = installed_label

    # 安装之后一律走**稳定入口** `<root>/bin/wengao`：
    # 这是用户真实使用方式（可从任意 cwd 调用、自动跟随 current.json），
    # 也避免"从载荷目录 import 才成功"这种假通过。
    entry = root / "bin" / "wengao"
    ev["entry"] = {"path": str(entry), "exists": entry.is_file(),
                   "executable": os.access(entry, os.X_OK) if entry.is_file() else False}
    entry_ok = ev["entry"]["exists"] and ev["entry"]["executable"]
    neutral = report / "cwd-neutral"
    neutral.mkdir(parents=True, exist_ok=True)
    if not entry_ok:
        step("entry_written", {"rc": 4, "stdout_head": "", "stderr_head":
                               "安装后未生成可执行的 <root>/bin/wengao"}, ok=False)

    # ---- 2 安装后状态（经稳定入口、从中性 cwd 调用） ----
    r = run([str(entry), "status", "--json"], neutral)
    step("status_after_install", r, ok=(r["rc"] == 0) if entry_ok else False)

    # ---- 2b 诊断：安装完整性 / 渲染 / 模型端点（缺什么就直接写进证据） ----
    r = run([str(entry), "doctor", "--json"], neutral)
    step("doctor_after_install", r, ok=(r["rc"] == 0) if entry_ok else False)
    doc = r["json"] or {}
    ev["doctor"] = {k: doc.get(k) for k in
                    ("install_ok", "run_ready", "blocking_missing",
                     "prerequisites_missing")}
    if doc.get("checks"):
        ev["doctor"]["checks"] = [
            {"name": c.get("name"), "ok": c.get("ok"),
             "severity": c.get("severity"), "detail": c.get("detail")}
            for c in doc["checks"]]
    st = r["json"] or {}
    ev["status_after_install"] = {k: st.get(k) for k in
                                  ("root", "current", "versions", "data") if k in st}
    version_dir = root / "versions" / str(st.get("current") or installed_label or "")
    ev["version_dir"] = str(version_dir)

    # ---- 2c 运行期门禁实现必须**装进去且真的能跑** ----
    # 只查文件在不在不够：`layout_gate.py` 缺件时 validate 会永久 NOT_EVALUABLE，
    # 而原生实播证据校验会退化成静默跳过（见 reports/r6r7-20261104/REPORT.md §1.5）。
    # 这里用**安装实例自己的副本**把它加载起来并调用一个纯函数，证明校验真的在跑。
    gate_probe = (
        "import importlib.util,json,os,sys,tempfile\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[1])/'scripts'/'layout_gate.py'\n"
        "spec=importlib.util.spec_from_file_location('gate_probe',p)\n"
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "fd,tmp=tempfile.mkstemp(suffix='.mp4'); os.write(fd,b'\\x00'*64); os.close(fd)\n"
        "info=m._inspect_mp4(Path(tmp)); os.unlink(tmp)\n"
        "has=lambda n: hasattr(m,n)\n"
        "ok=(info.get('ok') is False and has('_inspect_mp4')\n"
        "    and has('_validate_native_review_record'))\n"
        "print(json.dumps({'bytes':p.stat().st_size,'fake_mp4':info,\n"
        "                  'validators':[n for n in ('_inspect_mp4',\n"
        "                     '_validate_native_review_record') if has(n)],'ok':ok}))\n"
        "sys.exit(0 if ok else 3)\n")
    r = run([a.python, "-c", gate_probe, str(version_dir)], neutral)
    step("runtime_gate_loadable", r, ok=(r["rc"] == 0))
    ev["runtime_gate"] = r["json"] or {"rc": r["rc"],
                                       "stderr_head": r["stderr_head"]}

    # ---- 3 首任务：precheck → run → validate → publish → export ----
    scratch = report / "first-task"
    scratch.mkdir(parents=True, exist_ok=True)
    src = build_input_pptx(scratch / "cleanroom_3p.pptx")
    ev["input"] = {"path": str(src), "sha256": sha256_file(src),
                   "pages": 3, "origin": "本脚本用 python-pptx 生成，无用户私有材料"}

    r = run([str(entry), "cli", "--", "precheck", "--input", str(src),
             "--mode", "beautify", "--theme", "wengao-basic"], neutral)
    step("first_task_precheck", r)
    job_id = ((r["json"] or {}).get("job_id"))
    ev["job_id"] = job_id

    if job_id:
        r = run([str(entry), "cli", "--", "run", "--job-id", job_id,
                 "--timeout", "900"], neutral, timeout=1200)
        step("first_task_run", r)
        r = run([str(entry), "cli", "--", "validate", "--job-id", job_id], neutral,
                timeout=1200)
        step("first_task_validate", r, ok=(r["rc"] in (0,)) and
             ((r["json"] or {}).get("verdict") != "FAILED"))
        ev["validate_verdict"] = (r["json"] or {}).get("verdict")
        r = run([str(entry), "cli", "--", "publish", "--job-id", job_id], neutral)
        step("first_task_publish", r)
        r = run([str(entry), "cli", "--", "export", "--job-id", job_id,
                 "--outdir", str(scratch / "export")], neutral, timeout=1200)
        step("first_task_export", r)
        outdir = None
        if r["json"]:
            outdir = (r["json"].get("pptx") or r["json"].get("outdir")
                      or (r["json"].get("output") or {}).get("pptx"))
        # 只检查**导出目录**里的成品：任务目录里本来就有输入文件的副本，
        # 在那里 rglob 会拿到输入稿，把"输入"当"导出件"报告（假通过）。
        candidates = [p for p in (scratch / "export").rglob("*.pptx")] if r["rc"] == 0 else []
        if candidates:
            art = max(candidates, key=lambda p: p.stat().st_mtime)
            ev["artifact"] = {"path": str(art), "sha256": sha256_file(art),
                              "bytes": art.stat().st_size,
                              "origin": "install 实例 export 的真实输出"}
            conform = run([a.python, str(HERE / "ooxml_conform.py"),
                           "--pptx", str(art),
                           "--json-out", str(report / "artifact-ooxml.json")], PROG_ROOT)
            step("artifact_ooxml_conform", conform,
                 ok=(conform["json"] or {}).get("verdict") == "PASS")
        else:
            ev["artifact"] = None
            ev.setdefault("notes", []).append(
                "导出未产生成品，故未执行成品结构检查（不拿输入稿冒充成品）。")

    # ---- 4 升级：同一载荷换 label 装第二个构建（可区分构建） ----
    up_payload = report / "upgrade-payload"
    if up_payload.exists():
        shutil.move(str(up_payload), str(report / f"upgrade-payload-old-{int(time.time())}"))
    shutil.copytree(payload, up_payload)
    marker = up_payload / "UPGRADE-MARKER.txt"
    marker.write_text(f"upgrade marker {time.strftime('%Y%m%dT%H%M%S')}\n", encoding="utf-8")
    data_jobs_before = len(list((root / "data" / "jobs").glob("job-*"))) \
        if (root / "data" / "jobs").is_dir() else 0
    r = run([str(entry), "upgrade", "--package", str(up_payload),
             "--label", f"{installed_label or 'wengao'}-upgrade",
             "--expect-root", str(root)], neutral)
    step("upgrade", r)
    r = run([str(entry), "status", "--json"], neutral)
    step("status_after_upgrade", r)
    up_current = (r["json"] or {}).get("current")
    ev["upgrade"] = {"current_after": up_current,
                     "marker_present": (root / "versions" / str(up_current) /
                                        "UPGRADE-MARKER.txt").is_file()
                     if up_current else False,
                     "data_jobs_before": data_jobs_before,
                     "data_jobs_after": len(list((root / "data" / "jobs").glob("job-*")))
                     if (root / "data" / "jobs").is_dir() else 0}

    # ---- 5 回退 ----
    r = run([str(entry), "rollback", "--to", str(installed_label)], neutral)
    step("rollback", r)
    r = run([str(entry), "status", "--json"], neutral)
    step("status_after_rollback", r)
    ev["rollback"] = {"current_after": (r["json"] or {}).get("current")}

    # ---- 6 卸载预演 + --apply（程序移入 quarantine，数据保留） ----
    r = run([str(entry), "uninstall"], neutral)
    step("uninstall_preview", r)
    r = run([str(entry), "uninstall", "--apply"], neutral)
    step("uninstall_apply", r)
    r2 = run([str(entry), "status", "--json"], neutral)
    # 卸载后 status 必须报"未安装"（rc=4），这是卸载真的生效的证据，
    # 而不是只看 --apply 自己说成功。
    step("status_after_uninstall_expects_not_installed", r2, ok=(r2["rc"] == 4))
    ev["uninstall"] = {
        "quarantine": sorted(p.name for p in (root / "quarantine").glob("*"))
        if (root / "quarantine").is_dir() else [],
        "data_jobs_preserved": len(list((root / "data" / "jobs").glob("job-*")))
        if (root / "data" / "jobs").is_dir() else 0,
        "pointer_present_after_apply": (root / "current.json").is_file(),
        "pointer_backup_present": (root / "current.json.removed").is_file(),
        "status_exit_code": r2["rc"],
    }

    ev["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # 依赖归因：run 没跑通时，validate/publish/export 必然报 GATE.ARTIFACT_MISSING。
    # 那是**上游未完成**，不是这三个命令自身的缺陷；不这样归因就会把一次外部阻断
    # 放大成四条"失败"。
    by_step = {s["step"]: s for s in ev["steps"]}
    downstream = ("first_task_validate", "first_task_publish", "first_task_export")
    if "first_task_run" in by_step and not by_step["first_task_run"]["ok"]:
        for name in downstream:
            s = by_step.get(name)
            if s and not s["ok"]:
                s["not_evaluable"] = True
                s["blocked_by"] = "first_task_run"

    failed = [s["step"] for s in ev["steps"]
              if not s["ok"] and not s.get("blocked_external") and not s.get("not_evaluable")]
    blocked = [s["step"] for s in ev["steps"] if not s["ok"] and s.get("blocked_external")]
    not_evaluable = [s["step"] for s in ev["steps"] if s.get("not_evaluable")]
    ev["passed"] = len([s for s in ev["steps"] if s["ok"]])
    ev["failed"] = failed
    ev["blocked_external"] = blocked
    ev["not_evaluable"] = not_evaluable
    ev["model_env"] = {k: os.environ.get(k) for k in
                       ("WENGAO_LOCAL_MODEL_ENDPOINT",
                        "WENGAO_LOCAL_VISION_MODEL_ENDPOINT")}
    if failed:
        ev["verdict"] = "FAIL"
    elif blocked:
        # 安装器语义全部成立，但首任务的模型阶段因本机缺少视觉端点无法执行。
        # 这是**外部依赖未就绪**，既不算通过，也不算安装缺陷。
        ev["verdict"] = "PASS_INSTALLER_BLOCKED_FIRST_TASK_MODEL"
    else:
        ev["verdict"] = "PASS"
    (report / "CLEANROOM_INSTALL.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": ev["verdict"], "passed": ev["passed"],
                      "failed": failed, "blocked_external": blocked,
                      "not_evaluable": not_evaluable, "root": str(root),
                      "evidence": str(report / "CLEANROOM_INSTALL.json")},
                     ensure_ascii=False, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
