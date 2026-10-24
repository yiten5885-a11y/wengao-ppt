#!/usr/bin/env python3
"""P0 最终视觉门禁：冻结候选导出全范围独立复跑（2026-10-23 工作包 5）。

对冻结导出 f92232e1（job-4d584240ec05 候选）执行：
  1. 身份冻结断言（运行前后 PPTX SHA 必须一致，防止检查过程改写被检件）；
  2. 全 13 页 layout_gate 检查（真实 LibreOffice 渲染 ≥1920px 全字/无字 PNG、
     HG01–HG09 静态门禁、HG10 原生实播证据机证、HG11 身份链、HG13 结构清单）；
  3. 基线 = job input_raw（HG01 文本/备注守恒从磁盘实算，source_fidelity_verified
     不接受调用者自报，固定传 False）；
  4. 图像语义/位置/裁切审计：scene_visual_plan.json 计划图 ↔ HG13 内嵌媒体
     blob SHA / 页位 / crop 逐项绑定。

不触碰 8821/8823 服务、workbench/server.py 及其他工作包文件。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import layout_gate as lg  # noqa: E402

OUT = ROOT / "reports/gate-p0-final-20261023/final-acceptance"
POLICY = json.loads(Path(
    "/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/outputs/"
    "稳稿_严格产品验收/排版硬门禁_20261021/排版硬门禁.json").read_text(encoding="utf-8"))

JOB = ROOT / "wb_loopback_data/jobs/job-4d584240ec05"
PPTX = JOB / "candidate/fc829139_v25-readable-13p-dedicated.themed.pptx"
EXPECT_SHA = "f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c"
BASELINE = JOB / "input_raw/fc829139_v25-readable-13p-dedicated.pptx"
NATIVE_EV = (JOB / "layout_gate_evidence" / EXPECT_SHA / "native_playback.json")
SCENE_PLAN = JOB / "candidate/scene_visual_plan.json"


def observe_meta() -> dict:
    """只读观测 8823 /api/meta 身份（不写不重启；失败如实记录）。"""
    try:
        raw = subprocess.run(
            ["curl", "-s", "-m", "3", "http://127.0.0.1:8823/api/meta"],
            capture_output=True, text=True, timeout=8).stdout
        meta = json.loads(raw or "{}")
        inst = meta.get("instance", {}) if isinstance(meta, dict) else {}
        return {"port": 8823, "label": inst.get("label"),
                "build_id_observed": inst.get("build_id"),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "note": "只读观测；8821 未触碰（本周期连接拒绝=未运行）"}
    except Exception as exc:  # noqa: BLE001
        return {"port": 8823, "error": f"{type(exc).__name__}: {exc}"[:160]}


def image_semantic_audit(gate_report: dict) -> dict:
    """图像语义/位置/裁切：计划 ↔ 实际内嵌媒体逐项绑定。

    判定口径（2026-10-23 P0）：
    - 场景图：内嵌 blob SHA=计划 SHA、页位=计划页、画布内、裁切必须为
      "零裁切"或"对称居中裁切"（cover 适配：l=r=0 且 t=b，非偏侧裁切），
      且按**裁后区域**核对宽高比与版位一致（≤2%）；
    - 其余内嵌图必须能在"计划 ∪ 基线原稿媒体"中找到出处——原稿设计图属
      policy 不变量 original_media_bytes，保留不是计划外；真正计划外=新增。
    """
    plan = json.loads(SCENE_PLAN.read_text(encoding="utf-8"))
    # 基线原稿媒体集合（原稿媒体不变量的出处）
    from pptx import Presentation
    base_shas = set()
    bprs = Presentation(str(BASELINE))
    for slide in bprs.slides:
        inv, _p = lg.structure_inventory(slide)
        for m in inv.get("media", []):
            base_shas.add(m.get("blob_sha256"))
    per_page = gate_report.get("per_page", {})
    embedded = {}   # page -> [media entries]
    for pg, pdata in per_page.items():
        for m in (pdata.get("structure", {}) or {}).get("media", []):
            embedded.setdefault(int(pg), []).append(m)
    results = []
    all_ok = True
    for key, img in (plan.get("images") or {}).items():
        rec = {"scene_key": key, "planned_page": img.get("page"),
               "planned_sha256": img.get("sha256"),
               "provider": img.get("provider"),
               "source_key": img.get("source_key")}
        # 1) 计划素材文件在盘且哈希一致
        src = Path(img.get("file", ""))
        rec["asset_on_disk"] = src.is_file()
        rec["asset_sha_matches"] = src.is_file() and lg.sha256(src) == img.get("sha256")
        # 2) 内嵌媒体哈希/页位绑定
        pg = img.get("page")
        matches = [m for m in embedded.get(pg, [])
                   if m.get("blob_sha256") == img.get("sha256")]
        rec["embedded_on_page"] = bool(matches)
        if matches:
            m = matches[0]
            rec["embedded_shape"] = m.get("shape")
            rec["embedded_box_in"] = m.get("box_in")
            crop = m.get("crop") or {}
            rec["crop_values"] = crop
            center_crop = (abs(crop.get("crop_left") or 0) < 1e-3
                           and abs(crop.get("crop_right") or 0) < 1e-3
                           and abs((crop.get("crop_top") or 0)
                                   - (crop.get("crop_bottom") or 0)) < 1e-3)
            rec["crop_kind"] = ("zero" if all(abs(v or 0.0) < 1e-9 for v in crop.values())
                                else ("center_cover" if center_crop else "asymmetric"))
            rec["crop_declared_ok"] = rec["crop_kind"] in ("zero", "center_cover")
            box = m.get("box_in") or [None] * 4
            rec["inside_canvas"] = all(
                v is not None and -0.001 <= v <= 13.334 for v in
                [box[0], box[1], box[2], box[3]])
            # 宽高比：按裁后可见区域核对（cover 适配下应与版位一致 ≤2%）
            try:
                w_px, h_px = (m.get("pixel_size") or [0, 0])
                bw = (box[2] - box[0]) if box[2] is not None and box[0] is not None else 0
                bh = (box[3] - box[1]) if box[3] is not None and box[1] is not None else 0
                l, r = crop.get("crop_left") or 0, crop.get("crop_right") or 0
                t, b = crop.get("crop_top") or 0, crop.get("crop_bottom") or 0
                vis_w = w_px * (1 - l - r)
                vis_h = h_px * (1 - t - b)
                if vis_w and vis_h and bw and bh:
                    rec["aspect_visible"] = round(vis_w / vis_h, 4)
                    rec["aspect_box"] = round(bw / bh, 4)
                    rec["aspect_preserved"] = abs(
                        rec["aspect_visible"] - rec["aspect_box"]) / rec["aspect_box"] < 0.02
            except Exception:  # noqa: BLE001
                pass
        else:
            rec["embedded_shape"] = None
            rec["crop_kind"] = None
            rec["crop_declared_ok"] = False
            rec["inside_canvas"] = False
            rec["aspect_preserved"] = False
        rec["ok"] = bool(rec["asset_sha_matches"] and rec["embedded_on_page"]
                         and rec["crop_declared_ok"] and rec["inside_canvas"]
                         and rec.get("aspect_preserved", False))
        all_ok = all_ok and rec["ok"]
        results.append(rec)
    # 反向：内嵌图必须能溯源到"计划 ∪ 基线原稿媒体"；都不在 = 计划外新增
    planned_shas = {img.get("sha256") for img in (plan.get("images") or {}).values()}
    unplanned = []
    for pg, ms in embedded.items():
        for m in ms:
            sha = m.get("blob_sha256")
            if sha not in planned_shas and sha not in base_shas:
                unplanned.append({"page": pg, "shape": m.get("shape"),
                                  "blob_sha256": sha})
    return {"policy_basis": SCENE_PLAN.name,
            "plan_sha256": lg.sha256(SCENE_PLAN),
            "baseline_unique_media": len(base_shas),
            "scene_images": results, "all_scene_images_ok": all_ok,
            "unplanned_embedded_images": unplanned,
            "unplanned_count": len(unplanned),
            "note": "unplanned=0 表示被检件相对『计划∪基线原稿』没有计划外图片；"
                    "场景图零裁切或对称居中裁切、裁后等比、页位与计划一致"}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sha_before = lg.sha256(PPTX)
    assert sha_before == EXPECT_SHA, f"冻结导出身份不符：{sha_before}"
    assert lg.sha256(BASELINE) == ("0edb7ea298bf20c66f735fa5c8b71613ac7ae653036ad2"
                                   "46c6c4420c380b5711"), "job 基线被改动，拒绝"
    native = json.loads(NATIVE_EV.read_text(encoding="utf-8"))

    pdf = lg.render_pdf(PPTX, OUT / "render")
    pf = lg.render_pngs(pdf, OUT / "render", "full")
    nt_dir = OUT / "notext"
    nt_dir.mkdir(parents=True, exist_ok=True)
    nt = lg.strip_text_pptx(PPTX, nt_dir / "frozen-notext.pptx")
    nt_pdf = lg.render_pdf(nt, nt_dir)
    pn = lg.render_pngs(nt_pdf, nt_dir, "notext")

    rep = lg.check(PPTX, pdf, pages=list(range(1, 14)), baseline=BASELINE,
                   expect_pptx_sha=EXPECT_SHA, policy=POLICY,
                   png_full=pf, png_notext=pn, static_only=False,
                   native_playback=native, source_fidelity_verified=False)
    sha_after = lg.sha256(PPTX)
    rep["frozen_identity_recheck"] = {"before": sha_before, "after": sha_after,
                                      "unchanged": sha_before == sha_after}
    rep["meta_observation"] = observe_meta()

    audit = image_semantic_audit(rep)
    (OUT / "gate-report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "image-semantic-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    print("frozen pptx sha:", sha_after[:16], "| unchanged:", sha_before == sha_after)
    print("verdict:", rep["verdict"], "| gates_hit:", rep["gates_hit"])
    print("counts:", json.dumps(rep["counts"], ensure_ascii=False))
    print("unverified:", Counter(x.get("type", x.get("reason", "?"))
                                 for x in rep["unverified"]))
    print("build_id (gate-computed):",
          (rep.get("identity", {}).get("build_identity") or {}).get("build_id"))
    print("meta_observation:", json.dumps(rep["meta_observation"], ensure_ascii=False))
    print("image audit all_ok:", audit["all_scene_images_ok"],
          "| unplanned:", audit["unplanned_count"])
    print("report:", OUT / "gate-report.json")
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
