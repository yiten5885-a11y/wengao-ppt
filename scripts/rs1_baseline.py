#!/usr/bin/env python3
"""scripts/rs1_baseline.py —— RS-1 本机基线重跑（任务书 RS-1 原文执行）。

目的：同一份 13 页 PPTX（sha256 be8614d9…4065f，mode=beautify）在 Mac mini 本机用
真实 Qwen + 本机 LibreOffice 完整跑一遍，逐页渲染并人工看图，建立"改动前"对照。

输入优先级：--input 指定 > 环境变量 RS1_INPUT > 常见位置自动探测。
输出：reports/rs-w4/rs1-baseline/
  run.json          各阶段耗时/退出码/失败码/产物哈希
  pages/pNN.png     候选逐页渲染图（LibreOffice + pdfium，render_real）
  REPORT.md         人工看图清单与结论模板（**必须人工填写**，不得由脚本代填）

用法（在 Mac mini 本机、项目根目录）：
  python scripts/rs1_baseline.py --input /path/to/13p.pptx
  python scripts/rs1_baseline.py --input /path/to/13p.pptx --skip-run   # 只验输入+渲染既有候选
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "rs-w4" / "rs1-baseline"
EXPECTED_SHA256 = "be8614d943d875c808e2ac08773da5e9cec783512aef7b6a525a75118954065f"
STAGE_TIMEOUT = {"precheck": 300, "run": 1500, "validate": 1800}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_input(explicit: str | None) -> Path | None:
    cands = []
    if explicit:
        cands.append(Path(explicit))
    import os
    env = os.environ.get("RS1_INPUT")
    if env:
        cands.append(Path(env))
    # 常见位置：上传目录 / 样例目录 / jobs 输入
    for base in (ROOT / "workbench_uploads", ROOT / "samples", ROOT / "jobs"):
        if base.is_dir():
            cands += sorted(base.rglob("*.pptx"))
    for c in cands:
        if c.is_file() and c.suffix.lower() == ".pptx":
            return c
    return None


def cli(*args, timeout=600):
    proc = subprocess.run(
        [sys.executable, "-m", "wengao_ppt.cli", "--project-root", str(ROOT), *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    tail_out = (proc.stdout or "")[-800:]
    return {"rc": proc.returncode, "stdout_tail": tail_out,
            "stderr_tail": (proc.stderr or "")[-400:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None)
    ap.add_argument("--skip-run", action="store_true", help="不跑 CLI，只渲染既有候选")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    run = {"started_at": now(), "expected_sha256": EXPECTED_SHA256, "stages": []}

    src = find_input(args.input)
    if src is None:
        print("RS-1 中止：找不到输入 PPTX。请用 --input 指定 13 页原稿。")
        return 2
    got = sha256_file(src)
    run["input"] = {"path": str(src), "sha256": got,
                    "sha256_match": got == EXPECTED_SHA256}
    print(f"输入：{src}\nsha256：{got}（{'匹配' if got == EXPECTED_SHA256 else '不匹配任务书记载值'}）")
    if got != EXPECTED_SHA256:
        print("警告：输入哈希与任务书 RS-1 记载不一致；若是同一稿件的不同拷贝请知悉，"
              "否则应停下来核对（宁缺勿错）。")

    if not args.skip_run:
        job_id = None
        for stage in ("precheck", "run", "validate"):
            t0 = time.monotonic()
            if stage == "precheck":
                r = cli("precheck", "--input", str(src), "--mode", "beautify",
                        timeout=STAGE_TIMEOUT[stage])
                try:
                    job_id = json.loads(r["stdout_tail"].splitlines()[-1]).get("job_id")
                except Exception:
                    job_id = None
            else:
                if not job_id:
                    run["stages"].append({"stage": stage, "skipped": "precheck 未产出 job_id"})
                    break
                r = cli(stage, "--job-id", job_id, timeout=STAGE_TIMEOUT[stage])
            r.update({"stage": stage, "elapsed_s": round(time.monotonic() - t0, 2),
                      "at": now(), "job_id": job_id})
            run["stages"].append(r)
            print(f"[{stage}] rc={r['rc']} 用时 {r['elapsed_s']}s")
            if r["rc"] != 0:
                print(f"阶段 {stage} 失败，停止后续阶段（如实记录，不掩盖）。")
                break

        # 找候选 PPTX 逐页渲染
        cand = None
        for base in (ROOT / "jobs",):
            for p in sorted(base.rglob("*.themed.pptx")) + sorted(base.rglob("candidate*.pptx")):
                cand = p
                break
            if cand:
                break
        run["candidate_pptx"] = str(cand) if cand else None
        if cand:
            run["candidate_sha256"] = sha256_file(cand)
    else:
        cand = src

    pages_dir = OUT / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    if cand and Path(cand).is_file():
        from wengao_ppt import render as render_mod
        t0 = time.monotonic()
        res = render_mod.render_real(Path(cand), pages_dir)
        run["render"] = {"elapsed_s": round(time.monotonic() - t0, 2), "result": res}
        pngs = sorted(pages_dir.glob("*.png"))
        run["render"]["png_count"] = len(pngs)
        print(f"渲染 {len(pngs)} 页 → {pages_dir}")
    else:
        print("未找到候选 PPTX，跳过渲染。")

    run["finished_at"] = now()
    (OUT / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    print(f"\nrun.json 已写入 {OUT / 'run.json'}")
    print("下一步（必须人工）：逐页查看 pages/*.png，把结论写进 REPORT.md。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
