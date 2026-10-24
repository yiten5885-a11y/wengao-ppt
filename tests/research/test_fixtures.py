"""tests/research/test_fixtures.py

验证交付给 W2 的 fixtures 格式完全符合公共契约 C4，且自带真实快照与校验通过。
"""

import json
import os
import pytest
from wengao_ppt.research.verify import verify_bundle


def test_fixture_files_exist_and_marked():
    fixture_dir = "fixtures/research"
    files = [
        "bundle_sample_zh.json",
        "bundle_sample_en.json",
        "bundle_sample_empty.json",
        "bundle_sample_conflict.json",
    ]
    for fn in files:
        fp = os.path.join(fixture_dir, fn)
        assert os.path.isfile(fp), f"Fixture 文件缺失: {fp}"
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("fixture") is True, f"{fn} 未按契约标注 fixture: true"
        assert data.get("schema") == "rs.research_bundle.v1"
        assert "bundle_id" in data
        assert "facts" in data
        assert "views" in data
        assert "queries" in data


def test_fixture_snapshots_grounded(tmp_path):
    # 将 fixture 复制到临时 job 目录测试 verify_bundle
    job_dir = str(tmp_path)
    evidence_res = os.path.join(job_dir, "evidence", "research")
    os.makedirs(evidence_res, exist_ok=True)

    # 复制 bundle_sample_zh.json 并修正 snapshot 路径相对路径
    with open("fixtures/research/bundle_sample_zh.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    # 在 job_dir 下建立 snapshots 目录
    os.makedirs(os.path.join(job_dir, "fixtures", "research", "snapshots"), exist_ok=True)
    for f_obj in data["facts"]:
        snap_src = f_obj["snapshot_path"]
        snap_dst = os.path.join(job_dir, snap_src)
        with open(snap_src, "r", encoding="utf-8") as sf:
            content = sf.read()
        with open(snap_dst, "w", encoding="utf-8") as df:
            df.write(content)

    with open(os.path.join(evidence_res, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    report = verify_bundle(job_dir)
    assert report["is_valid"] is True, f"Fixture zh 校验失败: {report['issues']}"
    assert report["passed_facts_count"] >= 4
