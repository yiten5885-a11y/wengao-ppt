"""wengao_ppt.pipeline.metrics —— 出稿率与场景覆盖率分开记账（C8 末行）。

规则：两个指标**不合并成一个 PASS**。
- deck_delivered：交付了能打开的 PPTX（存在且 python-pptx 可开）。
- scene_coverage：场景落在 T1–T3 的比例（FALLBACK_T4 不计入覆盖）。
- 另记录降级明细（t1/t2/t3/t4 计数）与检查结论，供报告与门禁重算。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def count_tiers(resolutions: list) -> dict:
    counts = {"t1": 0, "t2": 0, "t3": 0, "t4": 0}
    for r in resolutions or []:
        status = (r or {}).get("status", "")
        if status == "RESOLVED_T1":
            counts["t1"] += 1
        elif status == "RESOLVED_T2":
            counts["t2"] += 1
        elif status == "RESOLVED_T3":
            counts["t3"] += 1
        elif status == "FALLBACK_T4":
            counts["t4"] += 1
    return counts


def scene_coverage(resolutions: list) -> Optional[float]:
    total = len(resolutions or [])
    if total == 0:
        return None
    c = count_tiers(resolutions)
    covered = c["t1"] + c["t2"] + c["t3"]
    return round(covered / total, 4)


def deck_delivered(pptx_path: Optional[Path]) -> bool:
    if not pptx_path:
        return False
    p = Path(pptx_path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    try:
        from pptx import Presentation  # 延迟导入，测试环境无 pptx 时不炸
        Presentation(str(p))
        return True
    except Exception:  # noqa: BLE001
        return False


def summarize(pptx_path, resolutions, checks: Optional[dict] = None) -> dict:
    c = count_tiers(resolutions)
    return {
        "deck_delivered": deck_delivered(pptx_path),
        "scene_coverage": scene_coverage(resolutions),
        "tiers": c,
        "scene_total": sum(c.values()),
        "checks": checks or {},
    }
