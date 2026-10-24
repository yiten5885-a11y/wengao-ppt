"""W2 验收项 4：17 页回放材料上跑 W2 规划器，核对事实覆盖与分页缺陷。

对现有冻结回放包 `samples/material_replay/annual_review_17p.plan.json`
（2026-10-16 本地模型输出，17 页）做三项核对：

1. **必讲事实覆盖不下降**（`source_fidelity.check_fact_coverage` 口径）：
   - 基线 = 冻结计划自己声明覆盖的事实集（其 `must_state` 全文按
     `value_matches` 语义逐条核对）；
   - W2 = 同一材料跑 `plan_deck` → `compose_deck` 成稿后，用
     `check_fact_coverage` 对真实 pptx 逐条核对；
   - 通过条件：W2 覆盖数 ≥ 基线覆盖数，且无 BLOCKING finding。
2. **无"（续）"孤页**：冻结计划有 9 个"（续）"标题页（硬门禁禁止），
   W2 计划标题不得含"（续）"。
3. **无重复条目**：冻结计划首页同句出现两次；W2 计划同一节内不得有
   重复句子，全 deck 的要点也不得有跨页完全重复。

产出：`reports/rs-w2/replay_check.json` + `reports/rs-w2/replay_deck.*`。

用法：
    .venv/bin/python scripts/rs_w2_replay_check.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wengao_ppt import source_fidelity as SF  # noqa: E402
from wengao_ppt.layout import checks, compose  # noqa: E402
from wengao_ppt.planner import deck_plan, fact_ledger as FL  # noqa: E402

REPLAY_DIR = ROOT / "samples" / "material_replay"
MATERIAL = REPLAY_DIR / "annual_review_synthetic.md"
FROZEN_PLAN = REPLAY_DIR / "annual_review_17p.plan.json"

CONTINUATION_MARK = "（续）"


# ---------------------------------------------------------------- 基线口径
def frozen_plan_page_texts(plan: dict) -> list[str]:
    """冻结计划的每页"可见文本"= 标题 + must_state（回放计划没有 pptx）。"""
    texts = []
    for s in plan.get("slides") or []:
        parts = [str(s.get("title") or "")]
        parts += [str(x) for x in (s.get("must_state") or [])]
        texts.append("\n".join(p for p in parts if p))
    return texts


def fact_covered_by_texts(fact: dict, texts: list[str]) -> bool:
    """与 `check_fact_coverage` 同口径的"文本候选"覆盖判定（无图表/表格）。"""
    tok = SF.normalize(fact.get("raw_value_text") or "")
    if not tok:
        if fact.get("qualitative"):
            sent = SF.normalize((fact.get("source_ref") or {}).get("sentence") or "")
            core = sent.rstrip("。！？")
            full = SF.normalize("\n".join(texts))
            return bool(core) and core in full
        return True
    for t in texts:
        if SF.value_matches(fact, t)["ok"]:
            return True
    return False


def baseline_coverage(facts: list[dict], frozen: dict) -> dict:
    texts = frozen_plan_page_texts(frozen)
    covered, missing = [], []
    for f in facts:
        (covered if fact_covered_by_texts(f, texts) else missing).append(f["fact_id"])
    return {"total": len(facts), "covered": len(covered),
            "missing_ids": missing,
            "missing_detail": [
                {"fact_id": f["fact_id"],
                 "raw_value_text": f.get("raw_value_text"),
                 "sentence": (f.get("source_ref") or {}).get("sentence", "")[:60]}
                for f in facts if f["fact_id"] in set(missing)]}


# ---------------------------------------------------------------- 分页缺陷
def continuation_pages(plan: dict) -> list[dict]:
    out = []
    for p in plan.get("pages") or plan.get("slides") or []:
        title = str(p.get("title") or "")
        if CONTINUATION_MARK in title:
            out.append({"page": p.get("page") or p.get("index"),
                        "title": title,
                        "_continued_from": p.get("_continued_from")})
    return out


def duplicate_items(plan: dict) -> list[dict]:
    """同页内重复 + 跨页完全重复的条目。

    同时覆盖两种计划形状：W2 DeckPlan 的 `blocks[].items` 与冻结回放计划的
    `must_state`（回放计划没有 blocks）。
    """
    seen_global: dict[str, int] = {}
    dupes: list[dict] = []
    pages = plan.get("pages") or plan.get("slides") or []
    for p in pages:
        local: dict[str, int] = {}
        entries: list[str] = []
        for b in p.get("blocks") or []:
            entries += [str(x) for x in (b.get("items") or [])]
        entries += [str(x) for x in (p.get("must_state") or [])]
        for it in entries:
            key = it.strip()
            if not key:
                continue
            local[key] = local.get(key, 0) + 1
            seen_global[key] = seen_global.get(key, 0) + 1
        for key, n in local.items():
            if n > 1:
                dupes.append({"page": p.get("index") or p.get("page"),
                              "scope": "in_page", "times": n, "text": key[:60]})
    for key, n in seen_global.items():
        if n > 1:
            dupes.append({"page": None, "scope": "cross_page",
                          "times": n, "text": key[:60]})
    return dupes


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "rs-w2"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    material_text = MATERIAL.read_text(encoding="utf-8")
    frozen = json.loads(FROZEN_PLAN.read_text(encoding="utf-8"))
    facts = FL.ledger_from_plain_text(material_text,
                                      source_name=MATERIAL.name)
    ledger = {"facts": facts, "frozen_before_model_call": True,
              "source_file": str(MATERIAL)}

    report: dict = {
        "schema": "rs.w2_replay_check.v1",
        "material": str(MATERIAL),
        "material_sha256": __import__("hashlib").sha256(
            MATERIAL.read_bytes()).hexdigest(),
        "frozen_plan": str(FROZEN_PLAN),
        "facts_total": len(facts),
    }

    # ---- 1) 基线：冻结计划自报覆盖 -------------------------------------
    base = baseline_coverage(facts, frozen)
    report["baseline_frozen_17p"] = base
    print(f"[baseline] frozen 17p covers {base['covered']}/{base['total']}")

    # ---- 2) W2：plan_deck → compose_deck --------------------------------
    job_dir = out / "replay_job"
    plan = deck_plan.plan_deck(str(MATERIAL), None, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(job_dir))
    pptx = out / "replay_deck.pptx"
    compose_report = compose.compose_deck(plan, {}, "ink", str(pptx),
                                          place_image=None)
    report["w2_plan"] = {
        "pages": len(plan["pages"]),
        "page_list": [{"index": p["index"], "role": p["role"],
                       "variant": p.get("variant"), "title": p.get("title"),
                       "items": sum(len(b.get("items") or [])
                                    for b in p.get("blocks") or [])}
                      for p in plan["pages"]],
        "stats": plan["stats"],
        "warnings": plan["warnings"],
    }
    report["w2_compose"] = {"page_count": compose_report["page_count"],
                            "overflow": compose_report["overflow"],
                            "deck_sha256": compose_report["deck_sha256"]}

    # ---- 3) source_fidelity 口径核对 ------------------------------------
    src_findings = SF.check_fact_coverage(ledger, str(pptx), plan)
    blocking = [f for f in src_findings if f.get("severity") == "BLOCKING"]
    covered_ids = {f["fact_id"] for f in facts} - {
        f.get("fact_id") for f in blocking
        if f.get("origin") == "source_coverage"}
    report["w2_coverage"] = {
        "total": len(facts),
        "covered": len(covered_ids),
        "missing_ids": sorted({f.get("fact_id") for f in blocking
                               if f.get("origin") == "source_coverage"}),
        "findings": src_findings,
        "blocking_count": len(blocking),
    }
    print(f"[w2] covers {len(covered_ids)}/{len(facts)} "
          f"blocking={len(blocking)}")

    # ---- 4) 分页缺陷 ----------------------------------------------------
    w2_cont = continuation_pages(plan)
    w2_dupes = duplicate_items(plan)
    report["pagination_defects"] = {
        "frozen_continuation_pages": continuation_pages(frozen),
        "w2_continuation_pages": w2_cont,
        "frozen_duplicate_items": duplicate_items(frozen),
        "w2_duplicate_items": w2_dupes,
    }
    print(f"[pagination] frozen_cont={len(continuation_pages(frozen))} "
          f"w2_cont={len(w2_cont)} w2_dupes={len(w2_dupes)}")

    # ---- 5) 版式门禁 ----------------------------------------------------
    chk = checks.check_layout(str(pptx))
    report["layout_check"] = {"pass": chk["pass"], "page_count": chk["page_count"],
                              "findings": chk["findings"],
                              "size_rules_source": chk.get("size_rules_source")}
    print(f"[layout] pass={chk['pass']} pages={chk['page_count']} "
          f"findings={len(chk['findings'])}")

    # ---- 5b) 真实渲染（LibreOffice → PDF → PNG）-------------------------
    render_entry = {"pptx": pptx.name}
    try:
        from wengao_ppt import render as R  # noqa: PLC0415
        res = R.render_real(pptx, out / "replay_deck", dpi=110, timeout=300)
        pdf = res.get("pdf") or res.get("pdf_path")
        render_entry.update({"exit_code": res.get("exit_code"),
                             "timed_out": res.get("timed_out"),
                             "pdf": str(pdf) if pdf else None})
        if pdf and Path(pdf).exists():
            fams, err = R.pdf_font_families(pdf)
            pages = R.pdf_page_count(pdf)
            render_entry.update({
                "pdf_sha256": __import__("hashlib").sha256(
                    Path(pdf).read_bytes()).hexdigest(),
                "font_families": fams, "font_error": err,
                "pages": pages[0] if isinstance(pages, (tuple, list)) else pages,
                "cjk_stats": R.pdf_cjk_stats(pdf)})
        pngs = sorted((out / "replay_deck" / "png").glob("*.png")) \
            if (out / "replay_deck" / "png").exists() else []
        render_entry["png_count"] = len(pngs)
    except Exception as e:  # noqa: BLE001
        render_entry["error"] = f"{type(e).__name__}: {e}"
    report["render"] = render_entry
    print(f"[render] pdf_pages={render_entry.get('pages')} "
          f"png={render_entry.get('png_count')} "
          f"fonts={render_entry.get('font_families')}")

    # ---- 6) 判定 --------------------------------------------------------
    coverage_not_decreased = len(covered_ids) >= base["covered"]
    no_continuation = not w2_cont
    no_duplicates = not w2_dupes
    report["verdict"] = {
        "coverage_not_decreased": coverage_not_decreased,
        "coverage_delta": len(covered_ids) - base["covered"],
        "no_continuation_orphan": no_continuation,
        "no_duplicate_items": no_duplicates,
        "no_blocking_findings": not blocking,
        "layout_pass": chk["pass"],
        "rendered": (render_entry.get("png_count") == chk["page_count"]
                     and not render_entry.get("timed_out")
                     and not render_entry.get("error")),
        "pass": all([coverage_not_decreased, no_continuation, no_duplicates,
                     not blocking, chk["pass"]]),
    }
    (out / "replay_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("VERDICT:", json.dumps(report["verdict"], ensure_ascii=False))
    return 0 if report["verdict"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
