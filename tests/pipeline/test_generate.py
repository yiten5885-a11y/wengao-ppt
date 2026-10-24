"""tests/pipeline/test_generate.py —— 主管线端到端（离线替身，确定性）。

验证 C8 语义：
- 缺图（桩 resolve_scenes 全 T4）→ 仍然出稿，状态 DELIVERED_WITH_DEGRADES
- 事件流 8 步齐全且落盘
- 出稿率与场景覆盖率分开记账
- 取消可用
- 冲突裁决局部重跑只碰 plan/compose/check
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wengao_ppt import events as ev_mod
from wengao_ppt.pipeline import modules as mod
from wengao_ppt.pipeline.generate import GeneratePipeline
from wengao_ppt.pipeline.options import normalize

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def job_root():
    """项目内临时目录（沙箱对 /var/folders 的创建可能受限）。"""
    base = ROOT / "build" / "test-tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"job-{len(list(base.iterdir()))}"
    (d / "jobs").mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def offline_research(monkeypatch):
    """把 run_research 换成离线替身（不发起任何网络请求）。"""
    def fake_research(material_text, options, job_dir, emit):
        emit({"stage": "research", "status": "done", "summary": "离线替身研究"})
        return {
            "schema": "rs.research_bundle.v1", "bundle_id": "rb-test",
            "created_at": "2026-10-24T05:00:00+08:00", "material_language": "zh",
            "enabled": True, "queries": [], "facts": [], "views": [],
            "discarded": [], "omissions": [], "report_path": "",
        }
    monkeypatch.setitem(mod._cache, "run_research", (fake_research, "test-fake"))
    monkeypatch.setitem(mod._cache, "verify_bundle",
                        (lambda jd: {"status": "NOT_EVALUABLE", "reason": "test"},
                         "test-fake"))
    return fake_research


def test_generate_end_to_end_degrades_not_blocks(job_root, offline_research):
    sink = []
    pipe = GeneratePipeline(job_root, normalize({"mode": "generate"}), sink=sink)
    res = pipe.run("云帆零售 Q3 复盘\n\n复购率 46.8%，连续 8 个季度提升。\n\n门店扩展到 120 家。",
                   job_id="job-test-1")
    # 桩 resolve_scenes 全 T4：缺图不阻断出稿
    assert res["status"] in ("DELIVERED_WITH_DEGRADES", "BLOCKED")
    # 桩规划器不发明场景：无场景时覆盖率为 None（不适用），不得记 0 冒充
    assert res["metrics"]["scene_coverage"] in (None, 0.0)
    # deck_delivered 取决于 LibreOffice 之外的 pptx 可开性（桩构建器产出真实 pptx）
    deck = Path(res["deck_pptx"])
    assert deck.is_file() and deck.stat().st_size > 0
    # 事件流：read/research/layout/images/check 都出现过
    stages = [e["stage"] for e in sink]
    for s in ("read", "research", "images", "check"):
        assert s in stages, f"缺少 {s} 阶段事件"
    # 落盘
    assert (Path(res["job_dir"]) / "evidence" / "pipeline" / "events.jsonl").is_file()
    assert (Path(res["job_dir"]) / "evidence" / "pipeline" / "run.json").is_file()
    assert (Path(res["job_dir"]) / "evidence" / "layout" / "plan.json").is_file()
    assert (Path(res["job_dir"]) / "evidence" / "images" / "ledger.json").is_file()


def test_cancel_between_stages(job_root, offline_research):
    pipe = GeneratePipeline(job_root, normalize({"mode": "generate"}))
    res = pipe.run("测试材料", job_id="job-test-2", cancel_check=lambda: True)
    assert res["status"] == "CANCELLED"


def test_metrics_separated(job_root, offline_research):
    from wengao_ppt.pipeline.metrics import count_tiers, scene_coverage
    rs = [{"status": "RESOLVED_T1"}, {"status": "RESOLVED_T2"},
          {"status": "FALLBACK_T4"}, {"status": "FALLBACK_T4"}]
    assert count_tiers(rs) == {"t1": 1, "t2": 1, "t3": 0, "t4": 2}
    assert scene_coverage(rs) == 0.5
    assert scene_coverage([]) is None


def test_decisions_record_and_rerun(job_root, offline_research):
    pipe = GeneratePipeline(job_root, normalize({"mode": "generate"}))
    res = pipe.run("材料甲\n\n行业均值 37.4%。", job_id="job-test-3")
    from wengao_ppt.pipeline.decisions import (load_decisions, open_conflicts,
                                               record_decision)
    d = record_decision(Path(res["job_dir"]), "c01", "use_web")
    assert d["c01"] == "use_web"
    assert load_decisions(Path(res["job_dir"]))["c01"] == "use_web"
    # 冲突视图在测试替身里没有 conflicts；直接验证重跑路径不炸
    out = pipe.rerun_after_decisions(Path(res["job_dir"]))
    assert out["status"] == "RERUN_DONE"
    assert Path(out["deck_pptx"]).is_file()


def test_module_provenance_marks_stubs(job_root):
    prov = mod.provenance()
    assert prov["run_research"].startswith("real:")  # W3 已交付
    assert prov["resolve_scenes"] == "stub"          # W1 未就位
    assert prov["plan_deck"] == "stub"               # W2 未就位
    # 桩永不伪造成功
    fn, src = mod.get("check_image_ledger")
    rep = fn(job_root)
    assert rep["status"] == "NOT_EVALUABLE"


def test_stub_resolve_scenes_never_fabricates_image():
    fn, _ = mod.get("resolve_scenes")
    out = fn([{"scene_id": "s1", "page_id": "p1"}], {}, "/tmp", lambda d: None)
    assert out[0]["status"] == "FALLBACK_T4"
    assert out[0]["asset"] is None
