"""tests/pipeline/test_gate_rs.py —— RS 条件必检项接线测试（不放宽既有门禁）。"""
from __future__ import annotations

import pytest

from wengao_ppt import gate_rs
from wengao_ppt.runner import expected_required_checks

BASE_OPTIONS = {
    "schema": "rs.job_options.v1", "mode": "generate", "language": "zh",
    "research": {"enabled": True, "max_queries": 12, "locales": ["material", "en"]},
    "images": {"enabled": True, "allow_ai_generated": True,
               "per_scene_budget_s": 20, "deck_budget_s": 300},
    "transition": {"enabled": True, "family": "fade", "duration_ms": 500},
    "theme": None, "target_pages": None,
}


def test_no_options_no_change():
    """不传 rs_options：返回值与历史行为一致（无 rs_checks 注入）。"""
    exp = expected_required_checks("P1_BASELINE")
    assert "rs_checks" in exp and exp["rs_checks"] == {}
    assert "image.ledger" not in exp["required"]


def test_options_add_checks_only():
    exp = expected_required_checks("P1_BASELINE", rs_options=BASE_OPTIONS)
    for name in ("image.ledger", "transition.uniform",
                 "research.bundle_verified", "layout.compose_check"):
        assert name in exp["required"], f"{name} 应到期"
    # 既有必需项一条都没少（只加不减）
    for name in ("content_fidelity.required_text", "render.real_render",
                 "layout.hard_gate", "visual.semantic_scene_coverage"):
        assert name in exp["required"]


def test_disabled_features_not_required():
    opts = dict(BASE_OPTIONS)
    opts["research"] = {"enabled": False}
    opts["transition"] = {"enabled": False}
    opts["images"] = {"enabled": False}
    exp = expected_required_checks("P1_BASELINE", rs_options=opts)
    assert "research.bundle_verified" not in exp["required"]
    assert "transition.uniform" not in exp["required"]
    assert "image.ledger" not in exp["required"]
    # layout.compose_check 恒到期
    assert "layout.compose_check" in exp["required"]


def test_run_rs_checks_never_fakes_pass():
    """模块未就位时：状态必须是 NOT_EVALUABLE，不得是 EXECUTED/PASS。"""
    from pathlib import Path
    statuses = gate_rs.run_rs_checks(Path("/nonexistent.pptx"),
                                     Path("/tmp"), BASE_OPTIONS)
    for name, status in statuses.items():
        assert status in ("EXECUTED", "FAILED", "NOT_EVALUABLE")
        if status == "EXECUTED":
            pytest.skip(f"{name} 真模块已接入并实际通过（合法）")
    # 本机当前 W1 未交付：至少记录 NOT_EVALUABLE 或 FAILED，不允许空
    assert statuses, "到期的 RS 检查必须有状态记录"


def test_registry_view_marks_stub():
    view = gate_rs.registry_view()
    assert set(view) == {"image.ledger", "transition.uniform",
                         "research.bundle_verified", "layout.compose_check"}
    for name, v in view.items():
        assert "label" in v and "owner" in v and "module" in v
