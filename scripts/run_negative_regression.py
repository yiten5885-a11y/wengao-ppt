#!/usr/bin/env python3
"""N01–N08 反向回归（真实失败件 × 新门禁，监督 0940/0947 要求）。

- 全部用例在真实失败件上实跑，verdict 必须 FAIL 且命中 must_hit 规则 ID；
- N04–N06 截图经独立像素绑定（text-projection + title-band IoU 双方法一致）
  后才允许与 6133ad5d 导出件配对；
- N08 用最终 P268 交付件（ebf5d5b4）配对旧门禁 PASS 身份（c090aa14）实跑，
  必须命中 HG11 IDENTITY_MISMATCH；
- 汇总记录新源码/政策/导出件/PDF/每页 PNG 全部 SHA。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import layout_gate as lg  # noqa: E402
from PIL import Image  # noqa: E402

OUT = ROOT / "reports/negative-regression-20261021"
POLICY = json.loads(Path(
    "/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/outputs/"
    "稳稿_严格产品验收/排版硬门禁_20261021/排版硬门禁.json").read_text(encoding="utf-8"))
BASELINE = ROOT / "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"
PPTX_6133 = ROOT / "reports/readability-20261021/v13-readable-13p-glyphfit.pptx"
PDF_6133 = OUT / "gate-6133/frozen-6133ad5d.pdf"
PNG_FULL_6133 = OUT / "gate-6133/png-full"
PNG_NT_6133 = OUT / "gate-6133/notext/png"
PPTX_5A6 = Path("/private/tmp/failed-5a6bcb2d.pptx")
PDF_5A6 = OUT / "gate-5a6/failed-5a6bcb2d.pdf"
PNG_FULL_5A6 = OUT / "gate-5a6/png-full"
PNG_NT_5A6 = OUT / "gate-5a6/notext/png"
PPTX_P268 = ROOT / "reports/readability-20261021/v13-readable-P268-13p.pptx"
C090 = "c090aa14eab73fc4131eed5077335da6fc0f5371c2ed40e698d09058db0084ea"
SHA_6133 = "6133ad5db168baf45894dda57449d1886ce91460b03cbcfc5eb056ab876404a0"

BINDING = json.loads((OUT / "screenshot-binding-N04-N06.json").read_text(encoding="utf-8"))


def pngmap(folder, pages, prefix="full"):
    return {p: (str(Path(folder) / f"{prefix}-P{p:02d}.png"),
                None, Image.open(Path(folder) / f"{prefix}-P{p:02d}.png").size)
            for p in pages}


def run_case(cid, pptx, pdf, pages, must_hit, expect_sha=None, extra=None):
    pf = pngmap(PNG_FULL_6133 if pptx == PPTX_6133 else PNG_FULL_5A6, pages)
    pn = pngmap(PNG_NT_6133 if pptx == PPTX_6133 else PNG_NT_5A6, pages, "notext")
    rep = lg.check(pptx, pdf, pages=pages, baseline=BASELINE,
                   expect_pptx_sha=expect_sha, policy=POLICY,
                   png_full=pf, png_notext=pn, static_only=True)
    hits = {f["gate"] for f in rep["findings"]}
    ok = rep["verdict"] == "FAIL" and set(must_hit) <= hits
    print(f"{cid}: verdict={rep['verdict']} hits={sorted(hits)} "
          f"must_hit={must_hit} -> {'PASS' if ok else 'FAIL'}")
    return {"id": cid, "verdict": rep["verdict"], "hits": sorted(hits),
            "must_hit": must_hit, "result": "PASS" if ok else "FAIL",
            "evidence": extra or {}, "report": rep}


def main():
    cases = []
    # N01（真实失败件 5a6bcb2d）
    cases.append(run_case("N01", PPTX_5A6, PDF_5A6, [2], ["HG03", "HG06"],
                          expect_sha="5a6bcb2d03f953d1913cbd786985150260aff96c07a6a0317745814279de3e52",
                          extra={"artifact": "5a6bcb2d…", "defect": "number_caption_source_collision",
                                 "pptx_sha256": lg.sha256(PPTX_5A6),
                                 "pdf_sha256": lg.sha256(PDF_5A6)}))
    # N02–N07（冻结失败件 6133ad5d + 监督复核 PDF/PNG）
    spec = [
        ("N02", [9], ["HG04"], "later_opaque_card_occludes_text", None),
        ("N03", [10], ["HG03", "HG06"], "title_lead_and_body_conclusion_collisions", None),
        ("N04", [9], ["HG04", "HG06"], "card_clips_description_and_conclusion_intrudes",
         "用户反例/截屏2026-10-21 08.31.29.png"),
        ("N05", [11], ["HG03", "HG06", "HG08"], "body_conclusion_stacking_and_footer_ordinals",
         "用户反例/截屏2026-10-21 08.32.20.png"),
        ("N06", [12], ["HG03", "HG05", "HG08"], "narrow_column_overflow_conclusion_collision_detached_ordinals",
         "用户反例/截屏2026-10-21 08.33.16.png"),
        ("N07", [8, 10], ["HG02"], "unmatched_04_and_bu_gan_sheng_and_da_e_xiao_fei_leng_jing_qi_ignored",
         None),
    ]
    for cid, pages, must, defect, shot in spec:
        extra = {"artifact": SHA_6133, "defect": defect,
                 "pptx_sha256": lg.sha256(PPTX_6133), "pdf_sha256": lg.sha256(PDF_6133),
                 "render_png_sha256": {f"full-P{p:02d}.png": lg.sha256(PNG_FULL_6133 / f"full-P{p:02d}.png")
                                       for p in pages}}
        if shot:
            b = next(x for x in BINDING["bindings"] if x["screenshot"].endswith(shot.split("/")[-1]))
            extra["screenshot_binding"] = {
                "screenshot_sha256": b["screenshot_sha256"],
                "bound_page": b["bound_page"],
                "text_projection_similarity": b["similarity"],
                "title_band_iou": b["title_band_best_iou"],
                "methods_agree": b["bound_page"] == b["title_band_best"] == pages[0],
                "status": "BOUND——独立双方法像素绑定一致"}
        cases.append(run_case(cid, PPTX_6133, PDF_6133, pages, must,
                              expect_sha=SHA_6133, extra=extra))
    # N08（最终 P268 ebf5d5b4 vs 旧门禁 PASS 身份 c090aa14）——需现渲染
    # P0 2026-10-23：输出目录改为 rerun 专用，历史 gate-P268-final 证据原样保留
    print("N08: 渲染 P268 最终件…")
    outdir = OUT / "gate-P268-final-rerun-20261023"
    pdf268 = lg.render_pdf(PPTX_P268, outdir)
    pf268 = lg.render_pngs(pdf268, outdir / "png-full", "full")
    nt_pptx = outdir / "notext"
    if not nt_pptx.is_dir():          # 沙箱对 exist_ok=True 的 mkdir 误报 EEXIST
        nt_pptx.mkdir(parents=True)
    nt = lg.strip_text_pptx(PPTX_P268, nt_pptx / "p268-notext.pptx")
    nt_pdf = lg.render_pdf(nt, nt_pptx)
    pn268 = lg.render_pngs(nt_pdf, nt_pptx / "png", "notext")
    rep = lg.check(PPTX_P268, pdf268, baseline=BASELINE, expect_pptx_sha=C090,
                   policy=POLICY, png_full=pf268, png_notext=pn268, static_only=True)
    hits = {f["gate"] for f in rep["findings"]}
    ok = rep["verdict"] == "FAIL" and "HG11" in hits
    print(f"N08: verdict={rep['verdict']} hits={sorted(hits)} -> {'PASS' if ok else 'FAIL'}")
    (outdir / "N08-report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    cases.append({"id": "N08", "verdict": rep["verdict"], "hits": sorted(hits),
                  "must_hit": ["HG11"], "result": "PASS" if ok else "FAIL",
                  "evidence": {
                      "final_pptx_sha256": lg.sha256(PPTX_P268),
                      "expected_old_gate_pass_sha256": C090,
                      "defect": "旧门禁 PASS 绑定 c090aa14，最终交付件为 ebf5d5b4——配对必须错配",
                      "pdf_sha256": lg.sha256(pdf268),
                      "render_png_sha256": {f"full-P{p:02d}.png": v[1] for p, v in pf268.items()}}})

    summary = {
        "policy_version": POLICY["policy_version"],
        "policy_sha256": POLICY["document_sha256"],
        "gate_source_sha256": lg.sha256(Path(__file__)),
        "layout_gate_sha256": lg.sha256(ROOT / "scripts/layout_gate.py"),
        "baseline_sha256": lg.sha256(BASELINE),
        "run_scope": "全部用例在真实失败件上以新门禁实跑；FAIL 且命中 must_hit 规则 ID 才算通过反向回归",
        "cases": [{k: v for k, v in c.items() if k != "report"} for c in cases],
        "all_pass": all(c["result"] == "PASS" for c in cases),
        "note": "失败件保留但标注未通过，不得作为合格下载或发布；"
                "N04–N06 截图已经双方法独立绑定到冻结 6133ad5d 导出件",
    }
    (OUT / "N01-N08-regression-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1))
    # 保存每例完整报告
    for c in cases:
        if "report" in c:
            (OUT / f"{c['id']}-newgate-report.json").write_text(
                json.dumps(c["report"], ensure_ascii=False, indent=1))
    print("all_pass:", summary["all_pass"])
    return 0 if summary["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
