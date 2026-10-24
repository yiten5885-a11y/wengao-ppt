"""mfix2 冻结证据的**语义对齐修复**确定性回放（零模型调用，显式 replay）。

背景：mfix2（真实模型 8 chunk 全部成功）分页后 9 页 < 冻结下限 10，
verdict=PAGE_RANGE_UNSATISFIED、无 PPTX。根因是 make_plan_chunked 按
slides[i] **位置**对齐章节：章节 1 落在 cover、章节 2 落在 agenda，
12 页骨架坍缩成 8 页。修复 = align_skeleton_to_subjects（标题语义匹配）。

本脚本**不调用模型**：全部输入来自冻结的失败运行
  reports/r5-fix/wb_annual_review_synthetic_mfix2/{content_plan.json,fact_ledger.json}
  - global_plan 原始响应 → 12 页骨架（cover/agenda/8 内容页/summary/致谢）
  - chunk1..8 原始响应   → 8 个章节的 must_state（真实模型输出，冻结不改）
  - fact_ledger          → 57 条冻结事实（覆盖校验/构建解析用）
输出（显式标注 replay、model_calls=0，不是新模型能力证据）：
  reports/r5-fix/replay_semantic_fix/{content_plan.realigned.json,
  annual_review_synthetic.replay.pptx, run_record.json}
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

CAND = Path(__file__).resolve().parents[2]          # 候选根 build/wengao-visual-v13-material-fix
FIX_DIR = Path(__file__).resolve().parent           # reports/r5-fix/replay_semantic_fix 的上级
MFIX2 = FIX_DIR / "wb_annual_review_synthetic_mfix2"
OUT = FIX_DIR / "replay_semantic_fix"

sys.path.insert(0, str(CAND))

# —— 复用候选内的已验证生成模块（与 cli.from-material 同一查找口径）——
spec = importlib.util.spec_from_file_location(
    "r5_material_to_ppt", CAND / "samples" / "r5_material_to_ppt.py")
r5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r5)

from wengao_ppt import theme as theme_mod            # noqa: E402


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat()


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    frozen_plan = json.loads((MFIX2 / "content_plan.json").read_text(encoding="utf-8"))
    frozen_ledger = json.loads((MFIX2 / "fact_ledger.json").read_text(encoding="utf-8"))
    facts = frozen_ledger["facts"]
    trace = frozen_plan["_trace"]

    # 1) 冻结骨架（模型真实输出，不改一字）
    gp = [t for t in trace if t.get("phase") == "global_plan"][-1]
    skeleton = json.loads(gp["raw_response"])
    plan = {"deck_title": skeleton.get("deck_title") or "业务汇报",
            "slides": skeleton["slides"]}

    # 2) 章节顺序（冻结台账首次出现序 = 真实运行时的填充序）
    order: list[str] = []
    for f in facts:
        s = f.get("subject") or "unknown"
        if s not in order:
            order.append(s)

    # 3) 冻结 chunk 输出 → 章节序（chunk{i} = 第 i 章节，attempt=1 的成功响应）
    chunk_ms: dict[int, list[str]] = {}
    for i in range(1, len(order) + 1):
        ct = [t for t in trace
              if t.get("phase") == f"chunk{i}" and t.get("attempt") == 1]
        if not ct:
            raise SystemExit(f"[replay] 冻结 trace 缺少 chunk{i}（attempt=1）")
        chunk_ms[i] = json.loads(ct[-1]["raw_response"])["must_state"]

    # 4) **新逻辑**：语义对齐（本次修复的对象）
    slide_of_subject = r5.align_skeleton_to_subjects(plan, order)
    slides = plan["slides"]
    for i, subj in enumerate(order):
        sk = slides[slide_of_subject[i]]
        sk["title"] = str(subj)[:16]
        sk["must_state"] = chunk_ms[i + 1]
    plan["omitted_facts"] = []
    plan["_chunked"] = True
    plan["_replay_source"] = {
        "frozen_run": "reports/r5-fix/wb_annual_review_synthetic_mfix2",
        "content_plan_sha256": sha256_file(MFIX2 / "content_plan.json"),
        "fact_ledger_sha256": sha256_file(MFIX2 / "fact_ledger.json"),
        "model_calls": 0,
        "note": "骨架与 chunk 输出均为 mfix2 冻结证据；仅重放修复后的对齐/分页/构建逻辑。",
    }
    slides_after_alignment = len(plan["slides"])

    # 5) 计划校验（覆盖/重复/页型）与资产校验
    problems = r5.validate_plan(plan, 12, facts)
    if problems:
        print(json.dumps({"verdict": "PLAN_FAILED", "problems": problems},
                         ensure_ascii=False, indent=2))
        (OUT / "run_record.json").write_text(json.dumps(
            {"verdict": "PLAN_FAILED", "replay": True, "problems": problems,
             "finished_at": now_iso()}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return 1
    asset_problems = r5.validate_plan_assets(plan)
    if asset_problems:
        (OUT / "run_record.json").write_text(json.dumps(
            {"verdict": "PLAN_ASSET_INVALID", "replay": True,
             "problems": asset_problems, "finished_at": now_iso()},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # 6) 分页（修复后的容量口径）+ 冻结区间判定
    plan, pag = r5.paginate_plan(plan, facts)
    pag["pages_after"] = r5.expected_slide_count(plan)
    pag["in_range"] = not r5.validate_page_range(pag["pages_after"])
    (OUT / "content_plan.realigned.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    run = {"schema_version": "1.0.0", "replay": True,
           "started_at": now_iso(),
           "what": "mfix2 冻结证据 × 语义对齐修复 的确定性回放",
           "material": frozen_plan.get("material"),
           "material_sha256": frozen_plan.get("material_sha256"),
           "target_pages": 12, "model_calls": 0,
           "alignment": {
               "subjects": len(order),
               "slides_after_alignment": slides_after_alignment,
               "dropped_skeleton_slides": plan.get("_dropped_skeleton_slides"),
               "added_skeleton_slides": plan.get("_added_skeleton_slides"),
           },
           "pagination": pag}
    range_problems = r5.validate_page_range(pag["pages_after"])
    if range_problems:
        run["verdict"] = "PAGE_RANGE_UNSATISFIED"
        run["page_range_conflict"] = {"pages": pag["pages_after"],
                                      "range": list(r5.PAGE_RANGE),
                                      "problems": range_problems}
        run["finished_at"] = now_iso()
        (OUT / "run_record.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(run, ensure_ascii=False, indent=2))
        return 1

    # 7) 原生构建（候选内 wengao-dark 可读性主题）
    pptx_path = OUT / "annual_review_synthetic.replay.pptx"
    prs = r5.build_pptx(plan, facts, pptx_path, theme_mod)
    actual = len(prs.slides._sldIdLst)
    run["pptx"] = {"path": str(pptx_path), "sha256": sha256_file(pptx_path),
                   "slides": actual,
                   "slides_match_pagination": actual == pag["pages_after"]}
    run["verdict"] = ("GENERATED_PENDING_VALIDATION"
                      if actual == pag["pages_after"] else "PAGE_COUNT_MISMATCH")
    run["finished_at"] = now_iso()
    (OUT / "run_record.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: run[k] for k in
                      ("verdict", "alignment", "pagination", "pptx")},
                     ensure_ascii=False, indent=2, default=str))
    return 0 if run["verdict"] == "GENERATED_PENDING_VALIDATION" else 1


if __name__ == "__main__":
    raise SystemExit(main())
