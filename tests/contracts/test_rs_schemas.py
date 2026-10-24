"""tests/contracts/test_rs_schemas.py —— rs-contracts-v1 每份契约的正反例测试（Step 0.5）。

正例必须通过；反例必须被 ContractError 拒绝，且错误信息指向具体字段。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wengao_ppt.contracts_rs import ContractError, schema_ids, validate

ROOT = Path(__file__).resolve().parents[2]
FX = ROOT / "fixtures"


def load(rel: str):
    return json.loads((FX / rel).read_text(encoding="utf-8"))


POSITIVE_CASES = [
    ("rs.job_options.v1", "pipeline/job_options.positive.json"),
    ("rs.scene_request.v1", "media/scene_request.positive.json"),
    ("rs.scene_resolution.v1", "media/scene_resolution.positive.json"),
    ("rs.research_bundle.v1", "research/research_bundle.positive.json"),
    ("rs.deck_plan.v1", "layout/deck_plan.positive.json"),
]

NEGATIVE_CASES = [
    ("rs.scene_request.v1", "media/scene_request.negative.json"),
    ("rs.scene_resolution.v1", "media/scene_resolution.negative.json"),
    ("rs.research_bundle.v1", "research/research_bundle.negative.json"),
    ("rs.deck_plan.v1", "layout/deck_plan.negative.json"),
]


def test_schema_registry_complete():
    ids = schema_ids()
    for expected in ["job_options.v1", "event.v1", "scene_request.v1",
                     "scene_resolution.v1", "research_bundle.v1", "deck_plan.v1"]:
        assert expected in ids, f"缺少契约 {expected}"


@pytest.mark.parametrize("schema_id,rel", POSITIVE_CASES)
def test_positive_fixtures_pass(schema_id, rel):
    obj = load(rel)
    assert obj["schema"] == schema_id
    assert validate(obj) is obj


@pytest.mark.parametrize("schema_id,rel", NEGATIVE_CASES)
def test_negative_fixtures_rejected(schema_id, rel):
    obj = load(rel)
    with pytest.raises(ContractError) as ei:
        validate(obj)
    msg = str(ei.value)
    assert msg.startswith("$"), f"错误信息应带路径：{msg}"


def test_event_schema_roundtrip():
    ev = {"schema": "rs.event.v1", "job_id": "job-1", "seq": 3,
          "ts": "2026-10-24T05:00:00+08:00",
          "stage": "images", "status": "done", "summary": "9 张实拍，3 张生成，2 页未配图",
          "counts": {"t2": 9, "t3": 3, "t4": 2}, "refs": ["evidence/images/ledger.json"]}
    assert validate(ev) is ev


def test_event_summary_too_long_rejected():
    ev = {"schema": "rs.event.v1", "job_id": "job-1", "seq": 1,
          "ts": "2026-10-24T05:00:00+08:00",
          "stage": "check", "status": "done", "summary": "长" * 41}
    with pytest.raises(ContractError):
        validate(ev)


def test_unknown_stage_rejected():
    ev = {"schema": "rs.event.v1", "job_id": "job-1", "seq": 1,
          "ts": "2026-10-24T05:00:00+08:00",
          "stage": "publish", "status": "done", "summary": "x"}
    with pytest.raises(ContractError):
        validate(ev)


def test_scene_resolution_t4_requires_reason():
    obj = load("media/scene_resolution.negative.json")
    obj.pop("asset")
    obj["reason"] = "Pexels 候选相似度均低于阈值，场景要求真实照片禁止生成"
    assert validate(obj)["status"] == "FALLBACK_T4"


def test_scene_resolution_rejects_unknown_provider():
    obj = load("media/scene_resolution.positive.json")
    obj["asset"]["provider"] = "some_random_site"
    with pytest.raises(ContractError):
        validate(obj)


def test_research_bundle_rejects_model_guess_origin():
    obj = load("research/research_bundle.negative.json")
    with pytest.raises(ContractError) as ei:
        validate(obj)
    assert "origin" in str(ei.value)


def test_quote_grounding_against_snapshot_fixture():
    """正例快照 fixture 的 quote_span 必须能在快照文本里逐字找到（落地校验样例）。"""
    obj = load("research/research_bundle.positive.json")
    snap = (FX / obj["facts"][0]["snapshot_path"].replace("fixtures/", "")).read_text(encoding="utf-8")
    assert obj["facts"][0]["quote_span"] in snap
    # 反例：quote 与快照不符时必须拒绝
    neg = load("research/research_bundle.negative.json")
    assert neg["facts"][0]["quote_span"] not in snap
