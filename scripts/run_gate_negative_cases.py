#!/usr/bin/env python3
"""P0 门禁行为负例 NC01–NC11（2026-10-23 工作包 5：最终视觉门禁及身份绑定）。

与 N01–N08（真实历史失败件）互补：本 harness 用**受控合成夹具**验证检查器
本身的行为——每个负例构造一个已知缺陷或已知合法场景，断言门禁产生与
**目标对象绑定**的判定（finding/unverified JSON 中必须出现目标对象名或
目标 token），不允许被无关 finding 代过，也不允许静默 PASS。

覆盖（用户 P0 清单）：
  NC01 页范围与完整覆盖（合法全页=正控；部分页=PARTIAL_PAGE_SCOPE 阻断）
  NC02 页号越界（ILLEGAL_PAGE_SELECTION）
  NC03 空页选择（EMPTY_PAGE_SELECTION）
  NC04 缺失渲染证据（MISSING_FULL_RENDER / MISSING_NOTEXT_RENDER，页绑定）
  NC05 错哈希（HG11 IDENTITY_MISMATCH）
  NC06 单对象文字遮挡（后画不透明容器全覆盖 → LATER_SHAPE_OCCLUSION）
  NC07 部分遮挡（半覆盖 → 部分字 invisible/partial + 遮挡命中）
  NC08 alpha 遮挡（alpha=0.92 后画容器 → 命中或按证据处置，绝不静默放行）
  NC09 图片遮挡（后画 Picture → 命中或 UNVERIFIED，绝不静默放行）
  NC10 继承字号（无显式字号 → HG07 RUN_SIZE_UNKNOWN/ROLE_SIZE）
  NC11 小数点与 token 断行（同基线 +0.9% 不得误报；真跨行 36个月 必须命中）

夹具只写入本报告目录（reports/gate-p0-final-20261023/negative-cases/fixtures/），
不修改任何原始输入/历史证据；渲染全部走 LibreOffice 真实路径（非 unit-test
桩），满足"不能仅跑检查器 unit tests"。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import layout_gate as lg  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Emu, Inches, Pt  # noqa: E402
from PIL import Image  # noqa: E402

OUT = ROOT / "reports/gate-p0-final-20261023/negative-cases"
FIX = OUT / "fixtures"
POLICY = json.loads(Path(
    "/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/outputs/"
    "稳稿_严格产品验收/排版硬门禁_20261021/排版硬门禁.json").read_text(encoding="utf-8"))
CANDIDATE = (ROOT / "wb_loopback_data/jobs/job-4d584240ec05/candidate/"
                   "fc829139_v25-readable-13p-dedicated.themed.pptx")
CANDIDATE_SHA = ("f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c")

TEXT_NAME = "P0目标文本"
OCC_NAME = "P0遮挡容器"
IMG_NAME = "P0遮挡图片"


# --------------------------------------------------------------------------- 夹具
def _new_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs


def _add_text(slide, name, text, left, top, w, h, size=24, color=None):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(w), Inches(h))
    box.name = name
    run = box.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = True
    if color:
        from pptx.dml.color import RGBColor
        run.font.color.rgb = RGBColor.from_string(color)
    return box


def _add_rect(slide, name, left, top, w, h, rgb="FF0000", alpha=None):
    from lxml import etree
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rect = slide.shapes.add_shape(1, Inches(left), Inches(top), Inches(w), Inches(h))
    rect.name = name
    spPr = rect._element.spPr
    for tag in (f"{{{A}}}solidFill", f"{{{A}}}noFill"):
        el = spPr.find(tag)
        if el is not None:
            spPr.remove(el)
    fill = etree.SubElement(spPr, f"{{{A}}}solidFill")
    clr = etree.SubElement(fill, f"{{{A}}}srgbClr")
    clr.set("val", rgb)
    if alpha is not None:
        a = etree.SubElement(clr, f"{{{A}}}alpha")
        a.set("val", str(int(alpha * 100000)))
    return rect


def _add_picture(slide, name, png: Path, left, top, w, h):
    pic = slide.shapes.add_picture(str(png), Inches(left), Inches(top),
                                   Inches(w), Inches(h))
    pic.name = name
    return pic


def _opaque_png(path: Path, rgb=(200, 30, 30)):
    img = Image.new("RGB", (400, 200), rgb)
    img.save(str(path))


def fixture_deck(case: str, out: Path) -> Path:
    """按用例构造单页合成夹具（全部落在 fixtures/ 受控目录）。"""
    prs = _new_deck()
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    if case == "NC06-单对象文字遮挡":
        _add_text(slide, TEXT_NAME, "关键结论一句话验证", 1.0, 1.0, 5.0, 0.6)
        _add_rect(slide, OCC_NAME, 0.8, 0.8, 5.5, 1.2, rgb="228822", alpha=1.0)
    elif case == "NC07-部分遮挡":
        _add_text(slide, TEXT_NAME, "结论甲结论乙验证文字", 1.0, 1.0, 5.0, 0.6)
        _add_rect(slide, OCC_NAME, 3.6, 0.7, 3.0, 1.2, rgb="228822", alpha=1.0)
    elif case == "NC08-alpha遮挡":
        _add_text(slide, TEXT_NAME, "关键结论一句话验证", 1.0, 1.0, 5.0, 0.6)
        # 0.92 < 0.95：静态判据不可放行；重暗色近实心 → 像素证据应判不可见
        _add_rect(slide, OCC_NAME, 0.8, 0.8, 5.5, 1.2, rgb="111111", alpha=0.92)
    elif case == "NC08b-alpha透见":
        _add_text(slide, TEXT_NAME, "关键结论一句话验证", 1.0, 1.0, 5.0, 0.6)
        # 0.45 中灰罩黑字：文字透见 → 静态判据不可证，但逐字像素证据应消解
        _add_rect(slide, OCC_NAME, 0.8, 0.8, 5.5, 1.2, rgb="BFBFBF", alpha=0.45)
    elif case == "NC09-图片遮挡":
        png = FIX / "occluder.png"
        _opaque_png(png)
        _add_text(slide, TEXT_NAME, "关键结论一句话验证", 1.0, 1.0, 5.0, 0.6)
        _add_picture(slide, IMG_NAME, png, 0.8, 0.8, 5.5, 1.2)
    elif case == "NC10-继承字号":
        # 不设任何显式字号：run.font.size 不写 → 继承，不得静默 PASS
        box = slide.shapes.add_textbox(Inches(1.0), Inches(1.0), Inches(6), Inches(0.8))
        box.name = TEXT_NAME
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "继承字号验证段落"
    elif case == "NC11a-小数点同基线":
        _add_text(slide, TEXT_NAME, "同比+0.9%与2500亿规模", 1.0, 1.0, 6.0, 0.6)
    elif case == "NC11b-token真跨行":
        # 窄框强制 "36个月" 跨行（renderer 真实断行，非桩；1.4in 实测
        # 断成 "工期36/个月完/成"，token 携带字符全部保持映射）
        _add_text(slide, TEXT_NAME, "工期36个月完成", 1.0, 1.0, 1.4, 1.4, size=24)
    else:
        raise ValueError(case)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


# --------------------------------------------------------------------------- 执行
def render_and_check(pptx: Path, outdir: Path, pages=None, expect_sha=None,
                     drop_render_pages=(), baseline=None):
    outdir.mkdir(parents=True, exist_ok=True)
    pdf = lg.render_pdf(pptx, outdir / "render")
    pf = lg.render_pngs(pdf, outdir / "render", "full")
    nt_dir = outdir / "notext"
    nt_dir.mkdir(parents=True, exist_ok=True)
    nt = lg.strip_text_pptx(pptx, nt_dir / (pptx.stem + "-notext.pptx"))
    nt_pdf = lg.render_pdf(nt, nt_dir)
    pn = lg.render_pngs(nt_pdf, nt_dir, "notext")
    if drop_render_pages:
        pf = {p: v for p, v in pf.items() if p not in drop_render_pages}
        pn = {p: v for p, v in pn.items() if p not in drop_render_pages}
    return lg.check(pptx, pdf, pages=pages, baseline=baseline,
                    expect_pptx_sha=expect_sha, policy=POLICY,
                    png_full=pf, png_notext=pn, static_only=True)


def entry_refers(entry: dict, needles: list[str]) -> bool:
    blob = json.dumps(entry, ensure_ascii=False)
    return all(n in blob for n in needles)


def collect(rep: dict, gate: str, types: list[str] | None = None):
    items = [f for f in rep.get("findings", []) if f.get("gate") == gate]
    items += [u for u in rep.get("unverified", []) if u.get("gate") == gate]
    if types:
        items = [x for x in items if x.get("type") in types]
    return items


CASES: list[dict] = []


def case(cid, desc, fn):
    CASES.append({"id": cid, "desc": desc, "fn": fn})


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    FIX.mkdir(parents=True, exist_ok=True)
    results = []

    # ---- 候选件渲染一次，供 NC01–NC05 复用（真实渲染，非复用历史 PNG）----
    print("渲染冻结候选（NC01–NC05 共用）…")
    cand_dir = OUT / "render-candidate"
    cand_pdf = lg.render_pdf(CANDIDATE, cand_dir / "render")
    cand_pf = lg.render_pngs(cand_pdf, cand_dir / "render", "full")
    cand_nt_dir = cand_dir / "notext"
    cand_nt_dir.mkdir(parents=True, exist_ok=True)
    cand_nt = lg.strip_text_pptx(CANDIDATE, cand_nt_dir / "cand-notext.pptx")
    cand_nt_pdf = lg.render_pdf(cand_nt, cand_nt_dir)
    cand_pn = lg.render_pngs(cand_nt_pdf, cand_nt_dir, "notext")
    cand_sha = lg.sha256(CANDIDATE)
    assert cand_sha == CANDIDATE_SHA, "冻结候选身份变化，拒绝继续"

    def run_candidate(pages, drop=()):
        return lg.check(CANDIDATE, cand_pdf, pages=pages,
                        expect_pptx_sha=cand_sha, policy=POLICY,
                        png_full={p: v for p, v in cand_pf.items() if p not in drop},
                        png_notext={p: v for p, v in cand_pn.items() if p not in drop},
                        static_only=True)

    # NC01a：合法全页 = 完整覆盖正控（不得出现范围类异常）
    def nc01a():
        rep = run_candidate(list(range(1, 14)))
        bad = collect(rep, "HG10", ["PARTIAL_PAGE_SCOPE", "ILLEGAL_PAGE_SELECTION",
                                    "EMPTY_PAGE_SELECTION"])
        assert not bad, f"全页正控出现范围类条目: {bad[:2]}"
        assert rep["pages_checked"] == list(range(1, 14))
        return {"note": "1..13 全页：无 PARTIAL/ILLEGAL/EMPTY，覆盖=全部页",
                "pages_checked": rep["pages_checked"]}
    case("NC01a", "合法页范围/完整覆盖（正控）", nc01a)

    # NC01b：部分页 → PARTIAL_PAGE_SCOPE 且不构成 PASS
    def nc01b():
        rep = run_candidate(list(range(1, 13)))
        hits = collect(rep, "HG10", ["PARTIAL_PAGE_SCOPE"])
        assert rep["verdict"] != "PASS", "部分页不得 PASS"
        assert hits and entry_refers(hits[0], ["PARTIAL_PAGE_SCOPE"]), "缺 PARTIAL_PAGE_SCOPE"
        assert hits[0]["selected"] == list(range(1, 13))
        return {"verdict": rep["verdict"], "bound": hits[0]}
    case("NC01b", "部分页范围（1..12）不得构成合格判定", nc01b)

    # NC02：页号越界
    def nc02():
        rep = run_candidate([1, 14])
        hits = collect(rep, "HG10", ["ILLEGAL_PAGE_SELECTION"])
        assert rep["verdict"] == "FAIL", "越界必须 FAIL"
        assert hits and hits[0]["illegal_pages"] == [14], "必须绑定越界页 14"
        return {"verdict": rep["verdict"], "bound": hits[0]}
    case("NC02", "页号越界 14 → ILLEGAL_PAGE_SELECTION", nc02)

    # NC03：空页选择
    def nc03():
        rep = run_candidate([])
        hits = collect(rep, "HG10", ["EMPTY_PAGE_SELECTION"])
        assert rep["verdict"] == "FAIL", "空选择必须 FAIL"
        assert hits, "缺 EMPTY_PAGE_SELECTION"
        return {"verdict": rep["verdict"], "bound": hits[0]}
    case("NC03", "空页选择 → EMPTY_PAGE_SELECTION", nc03)

    # NC04：缺第 5 页渲染证据（页绑定）
    def nc04():
        rep = run_candidate(list(range(1, 14)), drop=(5,))
        mf = collect(rep, "HG02", ["MISSING_FULL_RENDER"])
        mn = collect(rep, "HG02", ["MISSING_NOTEXT_RENDER"])
        assert rep["verdict"] != "PASS"
        assert any(x.get("page") == 5 for x in mf), "缺 P5 MISSING_FULL_RENDER"
        assert any(x.get("page") == 5 for x in mn), "缺 P5 MISSING_NOTEXT_RENDER"
        return {"verdict": rep["verdict"],
                "missing_full_p5": [x for x in mf if x.get("page") == 5][0]}
    case("NC04", "缺失第 5 页渲染 → MISSING_*_RENDER 页绑定", nc04)

    # NC05：错哈希
    def nc05():
        rep = lg.check(CANDIDATE, cand_pdf, pages=list(range(1, 14)),
                       expect_pptx_sha="0" * 64, policy=POLICY,
                       png_full=cand_pf, png_notext=cand_pn, static_only=True)
        hits = collect(rep, "HG11", ["IDENTITY_MISMATCH"])
        assert rep["verdict"] == "FAIL"
        assert hits and hits[0]["expected"] == "0" * 64, "必须绑定错配哈希"
        return {"verdict": rep["verdict"], "bound": hits[0]}
    case("NC05", "期望哈希错配 → HG11 IDENTITY_MISMATCH", nc05)

    # NC06–NC11：合成夹具（真实渲染路径）
    for cid, desc, must in [
        ("NC06-单对象文字遮挡", "后画不透明容器全覆盖文本",
         {"finding": ("HG04", ["LATER_SHAPE_OCCLUSION"], [TEXT_NAME, OCC_NAME])}),
        ("NC07-部分遮挡", "后画不透明容器半覆盖文本",
         {"finding": ("HG04", ["LATER_SHAPE_OCCLUSION"], [TEXT_NAME, OCC_NAME]),
          "either": ("HG02", ["CHAR_INVISIBLE", "CHAR_PARTIAL_VISIBLE"], [TEXT_NAME])}),
        ("NC08-alpha遮挡", "alpha=0.92 后画容器（静态判据不可放行）",
         {"finding_or_unverified": ("HG04", ["LATER_SHAPE_OCCLUSION",
                                             "OCCLUSION_ALPHA_UNRESOLVED"],
                                    [TEXT_NAME, OCC_NAME])}),
        ("NC08b-alpha透见", "alpha=0.45 罩体下文字透见 → 必须证据化消解，不得记未证",
         {"resolved": [TEXT_NAME, OCC_NAME]}),
        ("NC09-图片遮挡", "后画 Picture 覆盖文本",
         {"finding_or_unverified": ("HG04", ["LATER_SHAPE_OCCLUSION",
                                             "OCCLUSION_CHECK_UNSUPPORTED"],
                                    [TEXT_NAME, IMG_NAME])}),
        ("NC10-继承字号", "无显式字号（继承）不得静默 PASS",
         {"finding_or_unverified": ("HG07", ["RUN_SIZE_UNKNOWN", "ROLE_SIZE"],
                                    [TEXT_NAME])}),
        ("NC11a-小数点同基线", "+0.9% 同基线不得误报 TOKEN_SPLIT（小数点修复正控）",
         {"must_not": ("HG08", ["TOKEN_SPLIT"], [TEXT_NAME, "+0.9%"])}),
        ("NC11b-token真跨行", "窄框强制 36个月 真跨行必须命中 TOKEN_SPLIT",
         {"finding": ("HG08", ["TOKEN_SPLIT"], [TEXT_NAME, "36个月"])}),
    ]:
        def make_fn(cid=cid, must=must):
            def fn():
                fx = fixture_deck(cid, FIX / f"{cid}.pptx")
                rep = render_and_check(fx, OUT / f"case-{cid}",
                                       pages=[1], expect_sha=lg.sha256(fx))
                assert rep["verdict"] != "PASS", f"{cid} 夹具不得静默 PASS"
                out = {"verdict": rep["verdict"]}
                if "finding" in must:
                    gate, types, needles = must["finding"]
                    hits = [x for x in collect(rep, gate, types)
                            if entry_refers(x, needles)]
                    assert hits, f"{cid}: 缺绑定 {needles} 的 {gate}/{types} finding"
                    out["bound_finding"] = hits[0]
                if "either" in must:
                    gate, types, needles = must["either"]
                    hits = [x for x in collect(rep, gate, types)
                            if entry_refers(x, needles)]
                    assert hits, f"{cid}: 缺绑定 {needles} 的 {gate} 证据"
                    out["bound_either"] = hits[0]
                if "finding_or_unverified" in must:
                    gate, types, needles = must["finding_or_unverified"]
                    hits = [x for x in collect(rep, gate, types)
                            if entry_refers(x, needles)]
                    assert hits, f"{cid}: 缺绑定 {needles} 的 {gate} finding/unverified"
                    out["bound"] = hits[0]
                if "resolved" in must:
                    needles = must["resolved"]
                    uv_hits = [x for x in rep.get("unverified", [])
                               if entry_refers(x, needles)]
                    resolved = [r for r in
                                (rep.get("per_page", {}).get("1", {})
                                 .get("occlusion_resolved") or [])
                                if entry_refers(r, needles)]
                    assert not uv_hits, f"{cid}: 透见情形不得记未证 {uv_hits[:1]}"
                    assert resolved, f"{cid}: 缺证据化消解台账（occlusion_resolved）"
                    assert resolved[0].get("min_stroke_coverage", 0) >= 0.70, \
                        "消解必须绑定 ≥0.70 笔画覆盖证据"
                    out["occlusion_resolved"] = resolved[0]
                if "must_not" in must:
                    gate, types, needles = must["must_not"]
                    hits = [x for x in collect(rep, gate, types)
                            if entry_refers(x, needles)]
                    assert not hits, f"{cid}: 目标被误报 {hits[:1]}"
                    resolved = [r for r in
                                (rep.get("per_page", {}).get("1", {})
                                 .get("occlusion_resolved") or [])
                                if r.get("text_shape") == TEXT_NAME]
                    out["no_false_token_split"] = True
                    if resolved:
                        out["occlusion_resolved_evidence"] = resolved
                return out
            return fn
        case(cid, desc, make_fn())

    # ---- 顺序执行并落盘 ----
    all_ok = True
    for c in CASES:
        try:
            ev = c["fn"]()
            results.append({"id": c["id"], "desc": c["desc"],
                            "result": "PASS", "evidence": ev})
            print(f"{c['id']}: PASS — {c['desc']}")
        except AssertionError as exc:
            all_ok = False
            results.append({"id": c["id"], "desc": c["desc"],
                            "result": "FAIL", "error": str(exc)[:400]})
            print(f"{c['id']}: FAIL — {exc}")

    summary = {
        "policy_version": POLICY["policy_version"],
        "policy_sha256": POLICY["document_sha256"],
        "layout_gate_sha256": lg.sha256(ROOT / "scripts/layout_gate.py"),
        "candidate_pptx_sha256": cand_sha,
        "note": "合成负例夹具只落在 fixtures/；渲染走 LibreOffice 真实路径；"
                "断言与目标对象名/token 绑定，不被无关 finding 代过",
        "cases": results,
        "all_pass": all_ok,
    }
    (OUT / "NC-results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("all_pass:", all_ok, "| report:", OUT / "NC-results.json")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
