#!/usr/bin/env python3
"""P0 验收 manifest 构建/复核（2026-10-23 工作包 5：最终视觉门禁及身份绑定）。

把同一冻结候选导出的全部验收证据绑定进单一 manifest：
  build/source 身份（门禁自算 + 只读观测）、输入/基线、PPTX、PDF、
  逐页全字与无字渲染、对象清单、备注、媒体、动画/转场、图像语义审计、
  门禁报告、负例回归（N01–N08 + NC01–NC11b）。

所有哈希由本脚本在构建时**从磁盘重算**，不接受任何调用者自报；
verify 语义内建：任何"重算值 vs 门禁报告记录"不一致都会列为 binding
problems 并以非零退出。

用法：python scripts/build_p0_manifest.py [build]
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import layout_gate as lg  # noqa: E402
from pptx import Presentation  # noqa: E402

BASE = ROOT / "reports/gate-p0-final-20261023"
FA = BASE / "final-acceptance"
PPTX = (ROOT / "wb_loopback_data/jobs/job-4d584240ec05/candidate/"
              "fc829139_v25-readable-13p-dedicated.themed.pptx")
INPUT = (ROOT / "wb_loopback_data/jobs/job-4d584240ec05/input_raw/"
                "fc829139_v25-readable-13p-dedicated.pptx")
NATIVE_EV = (ROOT / "wb_loopback_data/jobs/job-4d584240ec05/layout_gate_evidence/"
                   "f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c/"
                   "native_playback.json")
MANIFEST = BASE / "ACCEPTANCE_MANIFEST.json"


def sha(p: Path) -> str:
    return lg.sha256(p)


def recompute_structure(pptx: Path) -> dict:
    """从磁盘被检件重算逐页对象/备注/媒体/动画/转场清单摘要。"""
    prs = Presentation(str(pptx))
    digests = {}
    notes = {}
    media_count = {}
    anim = {}
    problems: list = []
    for i, slide in enumerate(prs.slides, 1):
        inv, probs = lg.structure_inventory(slide)
        problems.extend(f"P{i}: {x}" for x in probs)
        digests[i] = hashlib.sha256(json.dumps(
            inv, ensure_ascii=False, sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()
        notes[i] = inv.get("notes")
        media_count[i] = len(inv.get("media", []))
        anim[i] = {"animations": inv.get("animations"),
                   "transition": inv.get("transition")}
    return {"per_page_inventory_sha256": {str(k): v for k, v in digests.items()},
            "per_page_notes": {str(k): v for k, v in notes.items()},
            "per_page_media_count": {str(k): v for k, v in media_count.items()},
            "per_page_animations_transitions": {str(k): v for k, v in anim.items()},
            "problems": problems}


def render_sha_map(folder: Path, prefix: str, pages) -> dict:
    out = {}
    for p in pages:
        f = folder / f"{prefix}-P{p:02d}.png"
        out[str(p)] = sha(f) if f.is_file() else None
    return out


def collect() -> dict:
    gate_path = FA / "gate-report.json"
    audit_path = FA / "image-semantic-audit.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    pages = list(range(1, 14))

    # ---- 从磁盘重算（与门禁同源方法，非自报） ----
    pptx_sha = sha(PPTX)
    input_sha = sha(INPUT)
    pdf_path = FA / "render" / "fc829139_v25-readable-13p-dedicated.themed.pdf"
    struct = recompute_structure(PPTX)
    try:
        from wengao_ppt.install import build_id as _bid
        build_id_now = _bid(ROOT)
    except Exception as exc:  # noqa: BLE001
        build_id_now = None
        struct["build_id_error"] = f"{type(exc).__name__}: {exc}"[:160]

    # 结构清单重算值必须与门禁报告记录一致（同源重算互证）
    gate_struct = {pg: (pdata.get("structure", {}) or {}).get("inventory_sha256")
                   for pg, pdata in gate.get("per_page", {}).items()}
    struct_match = all(gate_struct.get(str(p)) == struct["per_page_inventory_sha256"].get(p)
                       for p in map(str, pages))

    pdf_sha = sha(pdf_path) if pdf_path.is_file() else None
    gm = (gate.get("identity", {}) or {}).get("per_page_render_sha256", {})
    gn = (gate.get("identity", {}) or {}).get("per_page_notext_render_sha256", {})
    full_map = render_sha_map(FA / "render", "full", pages)
    nt_map = render_sha_map(FA / "notext", "notext", pages)

    m = {
        "schema": "WG-P0-ACCEPTANCE-MANIFEST/1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "frozen_export": {
            "path": str(PPTX),
            "sha256_recomputed": pptx_sha,
            "expected": gate.get("pptx_sha256"),
            "matches_gate_report": pptx_sha == gate.get("pptx_sha256"),
            "before_after_unchanged": (gate.get("frozen_identity_recheck") or {}).get(
                "unchanged"),
        },
        "input_baseline": {
            "path": str(INPUT),
            "sha256_recomputed": input_sha,
            "matches_gate_report": input_sha == (gate.get("identity") or {}).get(
                "baseline_pptx_sha256"),
        },
        "pdf": {
            "path": str(pdf_path),
            "sha256_recomputed": pdf_sha,
            "matches_gate_report": pdf_sha == gate.get("pdf_sha256"),
        },
        "build_source_identity": {
            "build_id_recomputed_now": build_id_now,
            "build_id_at_gate_time": ((gate.get("identity") or {})
                                      .get("build_identity") or {}).get("build_id"),
            "meta_observation": gate.get("meta_observation"),
            "drift_note": "候选树由多工作包并行维护；build_id 三处观测若不同即为真实"
                          "漂移记录。冻结导出件身份以 pptx_sha256 为准（本 manifest "
                          "构建时刻重算并与门禁报告互证，字节级稳定）。",
        },
        "renders": {
            "full_1920px": full_map,
            "notext_1920px": nt_map,
            "matches_gate_report": full_map == gm and nt_map == gn,
        },
        "structure_inventory_recomputed": struct,
        "structure_matches_gate_report": struct_match,
        "image_semantic_audit": {
            "path": str(audit_path),
            "sha256_recomputed": sha(audit_path),
            "all_scene_images_ok": audit.get("all_scene_images_ok"),
            "unplanned_count": audit.get("unplanned_count"),
        },
        "native_playback_evidence": {
            "path": str(NATIVE_EV),
            "evidence_sha256": sha(NATIVE_EV) if NATIVE_EV.is_file() else None,
            "video_sha256_declared": "59ae7d6e5558cb3e03b9b546967ee91c5837dd6bf"
                                     "4c282247fae2dd22a372448",
            "machine_validated_by_gate": bool(
                ((gate.get("hg10") or {}).get("native_playback_checks") or {})
                .get("machine_checks_pass")),
        },
        "gate_reports": {},
        "verdicts": {
            "final_acceptance": gate.get("verdict"),
            "n01_n08_rerun": json.loads((BASE / "negative-regression-rerun/"
                                         "N01-N08-regression-summary-20261023.json")
                                        .read_text(encoding="utf-8")).get("all_pass"),
            "nc_negative_cases": json.loads((BASE / "negative-cases/NC-results.json")
                                            .read_text(encoding="utf-8")).get("all_pass"),
            "dc135f_recheck": json.loads((BASE / "dc135f-recheck/gate-report.json")
                                         .read_text(encoding="utf-8")).get("verdict"),
        },
    }
    for rel in ["gate-report.json", "image-semantic-audit.json"]:
        p = FA / rel
        if p.is_file():
            m["gate_reports"][f"final-acceptance/{rel}"] = sha(p)
    for rel in ["negative-regression-rerun/N01-N08-regression-summary-20261023.json",
                "negative-regression-rerun/N01-N08-summary-20261021-historic.json",
                "negative-cases/NC-results.json",
                "dc135f-recheck/gate-report.json"]:
        p = BASE / rel
        if p.is_file():
            m["gate_reports"][rel] = sha(p)
    return m


def main() -> int:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    m = collect()
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    # 自检：关键绑定必须全部成立（磁盘重算 vs 门禁报告）
    problems = []
    fe = m["frozen_export"]
    if not fe["matches_gate_report"]:
        problems.append("pptx sha 与门禁报告不一致")
    if not m["input_baseline"]["matches_gate_report"]:
        problems.append("基线 sha 与门禁报告不一致")
    if not m["pdf"]["matches_gate_report"]:
        problems.append("pdf sha 与门禁报告不一致")
    if not m["renders"]["matches_gate_report"]:
        problems.append("逐页渲染 sha 与门禁报告不一致")
    if not m["structure_matches_gate_report"]:
        problems.append("结构清单重算与门禁报告不一致")
    if fe["sha256_recomputed"] != fe["expected"]:
        problems.append("冻结导出身份漂移")
    if m["structure_inventory_recomputed"].get("problems"):
        problems.append(f"结构清单存在问题: "
                        f"{m['structure_inventory_recomputed']['problems'][:3]}")
    print("manifest written:", MANIFEST)
    print("verdicts:", json.dumps(m["verdicts"], ensure_ascii=False))
    print("build_id now/gate/meta:",
          m["build_source_identity"]["build_id_recomputed_now"], "/",
          m["build_source_identity"]["build_id_at_gate_time"], "/",
          (m["build_source_identity"]["meta_observation"] or {}).get("build_id_observed"))
    print("binding problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
