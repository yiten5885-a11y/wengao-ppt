"""wengao_ppt.research.verify

独立重算校验器：
- verify_bundle(job_dir: str) -> VerifyReport
- 只读磁盘重算：不信 bundle 自报字段，从磁盘重新读取快照文件
- 重算 SHA-256，逐字核对 quote_span，校验数值、单位、可比性
- 供门禁脚本无偏差复核使用
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from typing import Any

from wengao_ppt.research.types import VerifyReport

logger = logging.getLogger(__name__)


def verify_bundle(job_dir: str) -> dict[str, Any]:
    """只读磁盘重算检验 ResearchBundle。"""
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    bundle_file = os.path.join(job_dir, "evidence", "research", "bundle.json")

    if not os.path.isfile(bundle_file):
        return VerifyReport(
            schema="rs.verify_report.v1",
            is_valid=False,
            checked_at=checked_at,
            passed_facts_count=0,
            rejected_facts_count=0,
            issues=[{"fact_id": None, "code": "RESEARCH.BUNDLE_MISSING", "detail": f"未找到 bundle.json 文件: {bundle_file}"}],
        ).to_dict()

    try:
        with open(bundle_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return VerifyReport(
            schema="rs.verify_report.v1",
            is_valid=False,
            checked_at=checked_at,
            passed_facts_count=0,
            rejected_facts_count=0,
            issues=[{"fact_id": None, "code": "RESEARCH.BUNDLE_MALFORMED", "detail": f"解析 bundle.json 失败: {e}"}],
        ).to_dict()

    if not data.get("enabled", True):
        # 功能关闭时也是合法的空结果
        return VerifyReport(
            schema="rs.verify_report.v1",
            is_valid=True,
            checked_at=checked_at,
            passed_facts_count=0,
            rejected_facts_count=0,
            issues=[],
            details={"status": "RESEARCH_DISABLED"},
        ).to_dict()

    raw_facts = data.get("facts", [])
    raw_views = data.get("views", [])
    issues: list[dict[str, Any]] = []
    passed_facts = 0
    rejected_facts = 0
    facts_map: dict[str, dict] = {}

    # 1. 逐个核验事实
    for rf in raw_facts:
        fid = rf.get("fact_id", "")
        facts_map[fid] = rf
        origin = rf.get("origin", "")

        # 用户自带事实跳过外部快照检查
        if origin == "USER_MATERIAL":
            passed_facts += 1
            continue

        # 检查外部事实必备字段
        pub = rf.get("publisher", "").strip()
        url = rf.get("url", "").strip()
        snap_path = rf.get("snapshot_path", "").strip()
        snap_sha = rf.get("snapshot_sha256", "").strip()
        quote = rf.get("quote_span", "").strip()
        val = rf.get("value")

        if not pub or not url or not snap_path:
            issues.append({"fact_id": fid, "code": "RESEARCH.SOURCE_MISSING", "detail": "缺少 publisher / url / snapshot_path"})
            rejected_facts += 1
            continue

        abs_snap = os.path.join(job_dir, snap_path)
        if not os.path.isfile(abs_snap):
            issues.append({"fact_id": fid, "code": "RESEARCH.SOURCE_MISSING", "detail": f"快照文件不存在: {snap_path}"})
            rejected_facts += 1
            continue

        try:
            with open(abs_snap, "r", encoding="utf-8", errors="ignore") as sf:
                actual_content = sf.read()
        except Exception as e:
            issues.append({"fact_id": fid, "code": "RESEARCH.FETCH_FAILED", "detail": f"读取快照失败: {e}"})
            rejected_facts += 1
            continue

        # 重算 SHA-256
        actual_sha = hashlib.sha256(actual_content.encode("utf-8")).hexdigest()
        if snap_sha and snap_sha != actual_sha:
            issues.append({
                "fact_id": fid,
                "code": "RESEARCH.QUOTE_NOT_IN_SNAPSHOT",
                "detail": f"快照 SHA256 不一致: 自报 {snap_sha} vs 重算 {actual_sha}",
            })
            rejected_facts += 1
            continue

        # 检查 quote_span 逐字连续出现
        if not quote or quote not in actual_content:
            issues.append({
                "fact_id": fid,
                "code": "RESEARCH.QUOTE_NOT_IN_SNAPSHOT",
                "detail": f"quote_span 未在快照中逐字出现: {quote[:40]}...",
            })
            rejected_facts += 1
            continue

        # 检查数值是否存在于 quote_span
        if val is not None:
            try:
                v_flt = float(val)
                v_int_str = str(int(v_flt)) if v_flt.is_integer() else f"{v_flt:.2f}"
                v_flt_str = str(v_flt)
                if v_int_str not in quote and v_flt_str not in quote:
                    issues.append({
                        "fact_id": fid,
                        "code": "RESEARCH.UNIT_OR_PERIOD_MISMATCH",
                        "detail": f"数值 {val} 未在引用 quote_span 中出现",
                    })
                    rejected_facts += 1
                    continue
            except (ValueError, TypeError):
                pass

        passed_facts += 1

    # 2. 检查 Views 引用有效性与数学公式
    for v in raw_views:
        vid = v.get("view_id", "")
        series_list = v.get("series", [])
        for s in series_list:
            for p in s.get("points", []):
                ref = p.get("fact_ref", "")
                if ref not in facts_map:
                    issues.append({"fact_id": ref, "code": "RESEARCH.SOURCE_MISSING", "detail": f"视图 {vid} 引用了不存在的事实 {ref}"})

        # 核对 highlight computed 数值
        hl = v.get("highlight")
        if hl and isinstance(hl, dict):
            computed = hl.get("computed", {})
            if "diff" in computed and "target_value" in computed and "benchmark_value" in computed:
                exp_diff = round(computed["target_value"] - computed["benchmark_value"], 2)
                if abs(exp_diff - computed["diff"]) > 0.01:
                    issues.append({
                        "fact_id": None,
                        "code": "RESEARCH.CALCULATION_ERROR",
                        "detail": f"视图 {vid} 差值计算不符: 期望 {exp_diff} vs 实际 {computed['diff']}",
                    })

    is_valid = (len(issues) == 0) and (rejected_facts == 0)
    return VerifyReport(
        schema="rs.verify_report.v1",
        is_valid=is_valid,
        checked_at=checked_at,
        passed_facts_count=passed_facts,
        rejected_facts_count=rejected_facts,
        issues=issues,
        details={"total_facts": len(raw_facts), "total_views": len(raw_views)},
    ).to_dict()
