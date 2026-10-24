"""W2-D5　原生构建器回环校验：建 → 存 → 抽 → 核。

构建器把对象写进 PPTX 只是第一步；本模块重新打开成品文件、用既有的
`charts.extract_charts_pptx` / `tables.extract_tables_pptx` 抽回模型，
逐项核对调用方给的数据——**只核一致性，不补数、不修数**。
任何不一致都如实报出来（roundtrip.ok = False），由调用方决定是否降级。
"""
from __future__ import annotations

from pathlib import Path


def _close(a, b, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a) == str(b)


def verify_chart(pptx_path, expected: dict) -> dict:
    """核对 PPTX 里的原生图表与 expected 一致。

    expected: {"categories": [str, ...], "series": {名称: [数值, ...]}}
    返回 {"ok": bool, "checks": [...], "mismatches": [...]}
    """
    from .. import charts as charts_mod
    from pptx import Presentation

    path = str(pptx_path)
    prs = Presentation(path)
    models = charts_mod.extract_charts_pptx(prs, path)
    result = {"ok": False, "chart_count": len(models), "checks": [], "mismatches": []}
    if not models:
        result["mismatches"].append("成品里没有任何原生图表")
        return result
    m = models[0]
    exp_cats = [str(c) for c in expected.get("categories") or []]
    exp_series = expected.get("series") or {}

    def _check(name, ok, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            result["mismatches"].append(f"{name}: {detail}")

    _check("类别一致", list(m.categories) == exp_cats,
           f"期望 {exp_cats} / 实得 {list(m.categories)}")
    got_names = sorted(m.series)
    _check("系列名一致", got_names == sorted(exp_series),
           f"期望 {sorted(exp_series)} / 实得 {got_names}")
    for name, vals in exp_series.items():
        cache = (m.cache or {}).get(name) or {}
        got = [cache.get(c) for c in exp_cats]
        ok = len(got) == len(vals) and all(_close(g, v) for g, v in zip(got, vals))
        _check(f"系列[{name}]显示缓存数值一致", ok,
               f"期望 {list(vals)} / 实得 {got}")
    # C11：内嵌工作簿必须与显示缓存一致（否则编辑时会变样）
    findings = charts_mod.check_chart_source_consistency(m)
    _check("内嵌工作簿与显示缓存一致", not findings,
           "; ".join(f["detail"] for f in findings) or "一致")
    result["ok"] = not result["mismatches"]
    return result


def verify_table(pptx_path, expected: dict) -> dict:
    """核对 PPTX 里的原生表格与 expected 逐格一致。

    expected: {"headers": [str, ...], "rows": [[str, ...], ...]}
    """
    from .. import tables as tables_mod
    from pptx import Presentation

    path = str(pptx_path)
    prs = Presentation(path)
    models = tables_mod.extract_tables_pptx(prs, path)
    result = {"ok": False, "table_count": len(models), "checks": [], "mismatches": []}
    if not models:
        result["mismatches"].append("成品里没有任何原生表格")
        return result
    m = models[0]
    exp_h = [str(x) for x in expected.get("headers") or []]
    exp_r = [[str(x) for x in row] for row in expected.get("rows") or []]
    got_h = [str(x) for x in m.headers]
    got_r = [[str(x) for x in row] for row in m.rows]
    if got_h == exp_h and got_r == exp_r:
        result["checks"].append({"name": "表头与逐格内容一致", "ok": True, "detail": ""})
        result["ok"] = True
    else:
        result["mismatches"].append(
            f"表头/单元格不一致：期望 {exp_h} + {exp_r} / 实得 {got_h} + {got_r}")
        result["checks"].append({"name": "表头与逐格内容一致", "ok": False,
                                 "detail": result["mismatches"][-1]})
    return result
