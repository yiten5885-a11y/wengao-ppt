#!/usr/bin/env python3
"""13 页专用版式 × layout_gate 全页迭代（0947 清单第 2 条）。

- 基线 exact_v12.pptx 只读（SHA 前后校验），输出 v14-readable-13p-dedicated.pptx；
- 每轮：重建 → LibreOffice 渲染 PDF（≥1920px PNG）→ 无字版渲染 → check(all 13)；
- 不触碰 6133 / P268（ebf5d5b4）/ 8821 / 8823；失败件不发布。
用法：python run_gate_13p.py [--pages 1,3,9] [--no-build]
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import layout_gate as lg  # noqa: E402
from pptx import Presentation  # noqa: E402
from wengao_ppt import readability as rb  # noqa: E402

BASELINE = ROOT / "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"
BASELINE_SHA = "e43dad9e36c07a78b5a4d8490448427e8802886730e4091f266e6a451d14ea9b"
OUT_DIR = ROOT / "reports/gate-13p-20261021"
PPTX = OUT_DIR / "v14-readable-13p-dedicated.pptx"
POLICY_PATH = Path(
    "/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/outputs/"
    "稳稿_严格产品验收/排版硬门禁_20261021/排版硬门禁.json")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build():
    assert sha256(BASELINE) == BASELINE_SHA, "baseline 被改动，拒绝构建"
    shutil.copyfile(BASELINE, PPTX)
    prs = Presentation(str(PPTX))
    info = rb.apply_readable_deck(prs)          # 13 页全部专用版式
    prs.save(str(PPTX))
    assert sha256(BASELINE) == BASELINE_SHA, "baseline 在构建后被改动"
    return info


def main() -> int:
    args = sys.argv[1:]
    pages = list(range(1, 14))
    if "--pages" in args:
        pages = [int(x) for x in args[args.index("--pages") + 1].split(",")]
    do_build = "--no-build" not in args
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if do_build:
        info = build()
        print("built:", PPTX, "sha:", sha256(PPTX)[:16],
              "pages:", info["pages_relayout"])
    pdf = lg.render_pdf(PPTX, OUT_DIR / "render")
    pf = lg.render_pngs(pdf, OUT_DIR / "render", "full")
    nt_pptx = OUT_DIR / "notext"
    nt_pptx.mkdir(parents=True, exist_ok=True)
    nt = lg.strip_text_pptx(PPTX, nt_pptx / PPTX.name)
    nt_pdf = lg.render_pdf(nt, nt_pptx)
    pn = lg.render_pngs(nt_pdf, nt_pptx, "notext")
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    rep = lg.check(PPTX, pdf, pages=pages, baseline=BASELINE,
                   expect_pptx_sha=sha256(PPTX), policy=policy,
                   png_full=pf, png_notext=pn, static_only=True)
    out = OUT_DIR / "gate-report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("verdict:", rep.get("verdict"))
    f = rep.get("findings", [])
    u = rep.get("unverified", [])
    print(f"findings={len(f)} unverified={len(u)}")
    from collections import Counter
    print("findings:", Counter(x["type"] for x in f))
    print("unverified:", Counter(x.get("type", x.get("reason", "?")) for x in u))
    for x in f[:40]:
        pg = x.get("page", "?")
        print(f"  F {x['gate']} {x['type']} P{pg} {x.get('shape', x.get('pair', ''))}"
              f" {x.get('sides', '')} {x.get('gap_in', '')}")
    for x in u[:30]:
        pg = x.get("page", "?")
        print(f"  U {x.get('gate','?')} {x.get('type', x.get('reason','?'))} P{pg} {x.get('shape', '')}")
    print("report:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
